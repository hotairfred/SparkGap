#!/usr/bin/env python3
"""
train_model.py — Train a CW decoder model using CTC loss.

Takes synthetic CW spectrograms and trains a CNN+GRU model to decode
the full Morse text (e.g., "CQ TEST NN5SD NN5SD").

Architecture: Spectrogram → CNN (time+freq pooling) → BiGRU → CTC → text

Supports precomputed spectrograms (.pt file) for fast training.
Run with --precompute first to generate the .pt file from WAVs.
"""

import json
import os
import sys
import wave
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, TensorDataset
from torch.nn.utils.rnn import pad_sequence


# CTC blank = 0, then our character set
CHARS = '-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/ '  # '-' is CTC blank placeholder
BLANK_IDX = 0
CHAR_TO_IDX = {c: i for i, c in enumerate(CHARS)}
IDX_TO_CHAR = {i: c for i, c in enumerate(CHARS)}
NUM_CLASSES = len(CHARS)  # 39

TARGET_FRAMES = 768  # ~6.1s at 4kHz/hop=32


def text_to_indices(text):
    """Encode text string to list of class indices (1-based, 0=blank)."""
    indices = []
    for c in text.upper():
        if c in CHAR_TO_IDX and CHAR_TO_IDX[c] != BLANK_IDX:
            indices.append(CHAR_TO_IDX[c])
    return indices


def ctc_greedy_decode(log_probs):
    """Greedy CTC decode: collapse repeats, remove blanks."""
    indices = log_probs.argmax(dim=-1)  # (T,)
    decoded = []
    prev = BLANK_IDX
    for idx in indices:
        idx = idx.item()
        if idx != prev and idx != BLANK_IDX:
            decoded.append(IDX_TO_CHAR[idx])
        prev = idx
    return ''.join(decoded)


def compute_spectrogram(samples, fft_size=128, hop=32):
    """Vectorized spectrogram computation."""
    n_frames = (len(samples) - fft_size) // hop + 1
    # Create frame indices
    indices = np.arange(fft_size)[None, :] + np.arange(n_frames)[:, None] * hop
    frames = samples[indices] * np.hanning(fft_size)
    spec = np.abs(np.fft.rfft(frames, axis=1))[:, :-1]  # drop Nyquist
    return np.log1p(spec).astype(np.float32)


def precompute_spectrograms(data_dir='training_data', output_dir='training_data'):
    """Precompute all spectrograms as float16 and save to .pt chunks.

    float16 halves memory: 50K × 768 × 64 × 2 bytes = ~4.9 GB.
    Fits in 7.7 GB RAM alongside PyTorch (~1 GB) and model (~0.5 GB).

    Saves in 10K-sample chunks to avoid OOM during precompute.
    Training loads and concatenates at startup.
    """
    with open(os.path.join(data_dir, 'labels.json')) as f:
        labels = json.load(f)

    n = len(labels)
    print(f"Precomputing {n} spectrograms as float16...", file=sys.stderr)

    chunk_size = 10000
    all_targets = []
    all_target_lens = []
    max_target_len = 0
    chunk_idx = 0

    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        chunk = np.zeros((end - start, TARGET_FRAMES, 64), dtype=np.float16)

        for j, i in enumerate(range(start, end)):
            info = labels[i]
            wav_path = os.path.join(data_dir, info['file'])
            w = wave.open(wav_path, 'rb')
            frames = w.readframes(w.getnframes())
            w.close()
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

            spec = compute_spectrogram(samples)
            if spec.shape[0] < TARGET_FRAMES:
                spec = np.pad(spec, ((0, TARGET_FRAMES - spec.shape[0]), (0, 0)))
            else:
                spec = spec[:TARGET_FRAMES]

            chunk[j] = spec.astype(np.float16)

            target = text_to_indices(info['text'])
            all_targets.append(target)
            all_target_lens.append(len(target))
            max_target_len = max(max_target_len, len(target))

        chunk_path = os.path.join(output_dir, f'specs_chunk_{chunk_idx}.pt')
        torch.save(torch.from_numpy(chunk), chunk_path)
        chunk_idx += 1
        print(f"  {end}/{n} → {chunk_path} ({os.path.getsize(chunk_path)/1e6:.0f} MB)", file=sys.stderr)
        del chunk

    # Save targets + metadata
    targets_padded = torch.zeros(n, max_target_len, dtype=torch.long)
    for i, target in enumerate(all_targets):
        targets_padded[i, :len(target)] = torch.tensor(target, dtype=torch.long)
    target_lens = torch.tensor(all_target_lens, dtype=torch.long)

    targets_path = os.path.join(output_dir, 'targets.pt')
    torch.save({
        'targets': targets_padded,
        'target_lens': target_lens,
        'n_samples': n,
        'n_chunks': chunk_idx,
        'target_frames': TARGET_FRAMES,
        'max_target_len': max_target_len,
    }, targets_path)

    print(f"Done. {chunk_idx} chunks + targets.pt ({n} samples)", file=sys.stderr)


class ChunkedF16Dataset(Dataset):
    """Dataset with float16 spectrograms stored as separate chunks in RAM.

    Never concatenates chunks — indexes across them directly.
    Peak RAM = largest single chunk (~938 MB) + overhead, not all chunks combined.
    """

    def __init__(self, chunks, targets, target_lens, chunk_size):
        self.chunks = chunks        # list of (chunk_n, T, F) float16 tensors
        self.chunk_size = chunk_size
        self.targets = targets
        self.target_lens = target_lens
        self.n_total = sum(c.size(0) for c in chunks)

    def __len__(self):
        return self.n_total

    def __getitem__(self, idx):
        chunk_idx = idx // self.chunk_size
        within_idx = idx % self.chunk_size
        spec = self.chunks[chunk_idx][within_idx].float().unsqueeze(0)  # (1, T, F)
        tlen = self.target_lens[idx].item()
        return (
            spec,
            self.targets[idx, :tlen],
            tlen,
        )


class CWDataset(Dataset):
    """Dataset of CW spectrograms with full text labels for CTC training."""

    def __init__(self, data_dir='training_data', max_samples=None):
        with open(os.path.join(data_dir, 'labels.json')) as f:
            self.labels = json.load(f)
        if max_samples:
            self.labels = self.labels[:max_samples]
        self.data_dir = data_dir

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        info = self.labels[idx]
        wav_path = os.path.join(self.data_dir, info['file'])

        w = wave.open(wav_path, 'rb')
        frames = w.readframes(w.getnframes())
        w.close()
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

        spec = compute_spectrogram(samples)  # (T, 64)

        # Pad/truncate to 768 frames (covers ~6.1s at 4kHz/hop=32 = 125 fps)
        target_frames = TARGET_FRAMES
        if spec.shape[0] < target_frames:
            spec = np.pad(spec, ((0, target_frames - spec.shape[0]), (0, 0)))
        else:
            spec = spec[:target_frames]

        text = info['text']
        target = text_to_indices(text)

        return (
            torch.tensor(spec).unsqueeze(0),  # (1, T, F)
            torch.tensor(target, dtype=torch.long),
            len(target),
        )


def collate_fn(batch):
    """Custom collate: pad spectrograms and targets to batch max."""
    specs, targets, target_lens = zip(*batch)
    specs = torch.stack(specs)  # all same size due to pad/truncate
    # Concatenate targets for CTC loss
    targets_cat = torch.cat(targets)
    target_lens = torch.tensor(target_lens, dtype=torch.long)
    return specs, targets_cat, target_lens


class CWDecoder(nn.Module):
    """CNN+BiGRU with CTC output for CW text decoding."""

    def __init__(self):
        super().__init__()
        # Input: (batch, 1, 768, 64) — time × freq spectrogram
        self.cnn = nn.Sequential(
            nn.Conv2d(1, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.MaxPool2d((2, 2)),    # → (32, 384, 32)

            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(),
            nn.MaxPool2d((2, 2)),    # → (64, 192, 16)

            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(),
            nn.MaxPool2d((1, 2)),    # → (128, 192, 8) — keep time, pool freq

            nn.Conv2d(128, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(),
            nn.MaxPool2d((1, 2)),    # → (128, 192, 4)
        )

        # After CNN: 128 channels × 4 freq bins = 512 features per time step
        self.rnn = nn.GRU(
            input_size=128 * 4,
            hidden_size=256,
            num_layers=2,
            batch_first=True,
            dropout=0.3,
            bidirectional=True,
        )

        self.fc = nn.Linear(512, NUM_CLASSES)  # 256*2 bidir → classes

    def forward(self, x):
        # x: (batch, 1, T, F)
        features = self.cnn(x)  # (batch, 128, T', 4)
        batch_size, channels, time_steps, freq = features.shape

        # Reshape: (batch, T', channels*freq)
        features = features.permute(0, 2, 1, 3).reshape(batch_size, time_steps, channels * freq)

        # RNN
        rnn_out, _ = self.rnn(features)  # (batch, T', 512)

        # Per-timestep character probabilities
        output = self.fc(rnn_out)  # (batch, T', NUM_CLASSES)
        return output


def train(data_dir='training_data', epochs=50, batch_size=16, resume=False, lr=0.001):
    """Train the CW decoder with CTC loss."""
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}", file=sys.stderr)

    # Try precomputed float16 chunks first
    targets_path = os.path.join(data_dir, 'targets.pt')
    chunk0_path = os.path.join(data_dir, 'specs_chunk_0.pt')
    if os.path.exists(chunk0_path) and os.path.exists(targets_path):
        print(f"Loading precomputed float16 spectrograms...", file=sys.stderr)
        data = torch.load(targets_path, weights_only=True)
        n_chunks = data['n_chunks']
        chunks = []
        total_bytes = 0
        for ci in range(n_chunks):
            cp = os.path.join(data_dir, f'specs_chunk_{ci}.pt')
            chunk = torch.load(cp, weights_only=True)
            chunks.append(chunk)
            total_bytes += chunk.nelement() * 2
            print(f"  Loaded chunk {ci} ({chunk.shape})", file=sys.stderr)
        chunk_size = chunks[0].size(0)  # samples per chunk (10000)
        dataset = ChunkedF16Dataset(chunks, data['targets'], data['target_lens'], chunk_size)
        print(f"  {data['n_samples']} samples in {n_chunks} chunks ({total_bytes/1e9:.1f} GB float16)", file=sys.stderr)
    else:
        print("Loading training data from WAVs (slow — run --precompute first)...", file=sys.stderr)
        dataset = CWDataset(data_dir)

    n_total = len(dataset)
    n_train = int(0.85 * n_total)
    n_val = n_total - n_train
    train_set, val_set = torch.utils.data.random_split(
        dataset, [n_train, n_val],
        generator=torch.Generator().manual_seed(42)
    )

    train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True,
                              num_workers=0, pin_memory=(device.type == 'cuda'),
                              collate_fn=collate_fn)
    val_loader = DataLoader(val_set, batch_size=batch_size, shuffle=False,
                            num_workers=0, pin_memory=(device.type == 'cuda'),
                            collate_fn=collate_fn)

    model = CWDecoder().to(device)
    start_epoch = 0

    # Resume from checkpoint
    if resume and os.path.exists('cw_decoder_ctc.pth'):
        ckpt = torch.load('cw_decoder_ctc.pth', map_location=device, weights_only=True)
        if isinstance(ckpt, dict) and 'model_state_dict' in ckpt:
            model.load_state_dict(ckpt['model_state_dict'])
            start_epoch = ckpt.get('epoch', 0)
            print(f"Resumed from epoch {start_epoch}", file=sys.stderr)
        else:
            model.load_state_dict(ckpt)
            print("Loaded weights (no epoch info)", file=sys.stderr)

    ctc_loss = nn.CTCLoss(blank=BLANK_IDX, zero_infinity=True)
    optimizer = optim.Adam(model.parameters(), lr=lr)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5, factor=0.5)

    param_count = sum(p.numel() for p in model.parameters())
    print(f"Model: {param_count:,} parameters", file=sys.stderr)
    print(f"Training: {n_train} train, {n_val} val, {epochs} epochs, batch {batch_size}", file=sys.stderr)
    print(f"CTC output time steps: 192 (768 input / 4 from time pooling)", file=sys.stderr)

    best_val_loss = float('inf')

    for epoch in range(start_epoch, start_epoch + epochs):
        # --- Training ---
        model.train()
        train_loss = 0
        n_batches = 0

        for specs, targets_cat, target_lens in train_loader:
            specs = specs.to(device)
            optimizer.zero_grad()
            output = model(specs)  # (batch, T', classes)

            # CTC expects (T, batch, classes)
            log_probs = output.permute(1, 0, 2).log_softmax(dim=2)
            T = log_probs.size(0)
            batch_size_actual = specs.size(0)
            input_lengths = torch.full((batch_size_actual,), T, dtype=torch.long)

            loss = ctc_loss(log_probs, targets_cat.to(device), input_lengths.to(device), target_lens.to(device))

            if torch.isfinite(loss):
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                optimizer.step()
                train_loss += loss.item()
            n_batches += 1

        avg_train_loss = train_loss / max(n_batches, 1)

        # --- Validation ---
        model.eval()
        val_loss = 0
        val_batches = 0
        char_correct = 0
        char_total = 0
        exact_match = 0
        total_samples = 0

        with torch.no_grad():
            for specs, targets_cat, target_lens in val_loader:
                specs = specs.to(device)
                output = model(specs)
                log_probs = output.permute(1, 0, 2).log_softmax(dim=2)
                T = log_probs.size(0)
                batch_size_actual = specs.size(0)
                input_lengths = torch.full((batch_size_actual,), T, dtype=torch.long)

                loss = ctc_loss(log_probs, targets_cat.to(device), input_lengths.to(device), target_lens.to(device))
                if torch.isfinite(loss):
                    val_loss += loss.item()
                val_batches += 1

                # Decode and measure accuracy
                offset = 0
                for b in range(batch_size_actual):
                    pred_text = ctc_greedy_decode(output[b].cpu())
                    tlen = target_lens[b].item()
                    true_indices = targets_cat[offset:offset + tlen].tolist()
                    true_text = ''.join(IDX_TO_CHAR[i] for i in true_indices)
                    offset += tlen

                    # Character accuracy (simple positional overlap)
                    min_len = min(len(pred_text), len(true_text))
                    for ci in range(min_len):
                        if pred_text[ci] == true_text[ci]:
                            char_correct += 1
                    char_total += max(len(true_text), 1)

                    if pred_text.strip() == true_text.strip():
                        exact_match += 1
                    total_samples += 1

        avg_val_loss = val_loss / max(val_batches, 1)
        char_acc = char_correct / max(char_total, 1) * 100
        exact_acc = exact_match / max(total_samples, 1) * 100

        scheduler.step(avg_val_loss)

        # Print progress
        if (epoch + 1) % 2 == 0 or epoch == start_epoch:
            # Show a few sample predictions
            model.eval()
            with torch.no_grad():
                sample_specs, sample_targets, sample_lens = next(iter(val_loader))
                sample_out = model(sample_specs.to(device))
                offset = 0
                examples = []
                for b in range(min(3, sample_specs.size(0))):
                    pred = ctc_greedy_decode(sample_out[b].cpu())
                    tlen = sample_lens[b].item()
                    true_idx = sample_targets[offset:offset + tlen].tolist()
                    true = ''.join(IDX_TO_CHAR[i] for i in true_idx)
                    offset += tlen
                    examples.append(f'    "{true}" → "{pred}"')

            print(f"Epoch {epoch+1}: train_loss={avg_train_loss:.4f} val_loss={avg_val_loss:.4f} "
                  f"char_acc={char_acc:.1f}% exact={exact_acc:.1f}% lr={optimizer.param_groups[0]['lr']:.6f}",
                  file=sys.stderr)
            for ex in examples:
                print(ex, file=sys.stderr)

        # Save checkpoint
        if (epoch + 1) % 10 == 0 or avg_val_loss < best_val_loss:
            if avg_val_loss < best_val_loss:
                best_val_loss = avg_val_loss
                save_path = 'cw_decoder_ctc_best.pth'
            else:
                save_path = 'cw_decoder_ctc.pth'
            torch.save({
                'model_state_dict': model.state_dict(),
                'epoch': epoch + 1,
                'val_loss': avg_val_loss,
                'char_acc': char_acc,
            }, save_path)
            if save_path.endswith('best.pth'):
                print(f"  New best model saved (val_loss={avg_val_loss:.4f})", file=sys.stderr)

    # Final save
    torch.save({
        'model_state_dict': model.state_dict(),
        'epoch': epoch + 1,
        'val_loss': avg_val_loss,
        'char_acc': char_acc,
    }, 'cw_decoder_ctc.pth')
    print(f"\nTraining complete. Final: char_acc={char_acc:.1f}% exact={exact_acc:.1f}%", file=sys.stderr)
    print(f"Best model: cw_decoder_ctc_best.pth", file=sys.stderr)
    return model


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Train CW decoder')
    parser.add_argument('--resume', action='store_true', help='Resume from checkpoint')
    parser.add_argument('--precompute', action='store_true', help='Precompute spectrograms to .pt file')
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--batch-size', type=int, default=16)
    parser.add_argument('--lr', type=float, default=0.001)
    args = parser.parse_args()

    if args.precompute:
        precompute_spectrograms()
    else:
        train(epochs=args.epochs, batch_size=args.batch_size, resume=args.resume, lr=args.lr)
