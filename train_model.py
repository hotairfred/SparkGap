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

    # Save targets + WPM + metadata
    targets_padded = torch.zeros(n, max_target_len, dtype=torch.long)
    for i, target in enumerate(all_targets):
        targets_padded[i, :len(target)] = torch.tensor(target, dtype=torch.long)
    target_lens = torch.tensor(all_target_lens, dtype=torch.long)

    # WPM labels — 0 means unknown (will be masked out of WPM loss)
    wpm_values = torch.tensor(
        [float(labels[i].get('wpm', 0) or 0) for i in range(n)],
        dtype=torch.float32
    )

    targets_path = os.path.join(output_dir, 'targets.pt')
    torch.save({
        'targets': targets_padded,
        'target_lens': target_lens,
        'wpm': wpm_values,
        'n_samples': n,
        'n_chunks': chunk_idx,
        'target_frames': TARGET_FRAMES,
        'max_target_len': max_target_len,
    }, targets_path)

    print(f"Done. {chunk_idx} chunks + targets.pt ({n} samples)", file=sys.stderr)


class ChunkedF16Dataset(Dataset):
    """Dataset with float16 spectrograms stored as separate chunk files on disk.

    Loads one chunk at a time — peak RAM = one chunk (~983 MB) regardless of
    total dataset size. Supports datasets larger than available RAM (e.g. 183K
    samples = 18 GB if loaded all at once, but only ~1 GB with lazy loading).
    """

    def __init__(self, chunk_paths, targets, target_lens, chunk_size, wpm=None):
        self.chunk_paths = chunk_paths  # list of file paths to .pt chunk files
        self.chunk_size = chunk_size
        self.targets = targets
        self.target_lens = target_lens
        self.wpm = wpm
        self.n_total = targets.size(0)
        self._cached_idx = -1
        self._cached_chunk = None

    def __len__(self):
        return self.n_total

    def __getitem__(self, idx):
        chunk_idx = idx // self.chunk_size
        within_idx = idx % self.chunk_size
        if chunk_idx != self._cached_idx:
            self._cached_chunk = torch.load(self.chunk_paths[chunk_idx], weights_only=True)
            self._cached_idx = chunk_idx
        spec = self._cached_chunk[within_idx].float().unsqueeze(0)  # (1, T, F)
        tlen = self.target_lens[idx].item()
        wpm = self.wpm[idx].item() if self.wpm is not None else 0.0
        return (
            spec,
            self.targets[idx, :tlen],
            tlen,
            wpm,
        )


class ChunkSequentialSampler:
    """Sampler that keeps samples contiguous within chunks to avoid disk thrash.

    Each epoch: shuffle chunk order, shuffle samples within each chunk.
    Result: exactly n_chunks disk reads per epoch instead of n_samples.
    """

    def __init__(self, dataset, shuffle=True):
        self.dataset = dataset
        self.shuffle = shuffle

    def __len__(self):
        return len(self.dataset)

    def __iter__(self):
        n_chunks = len(self.dataset.chunk_paths)
        chunk_size = self.dataset.chunk_size
        n_total = len(self.dataset)

        chunk_order = list(range(n_chunks))
        if self.shuffle:
            import random
            random.shuffle(chunk_order)

        for ci in chunk_order:
            start = ci * chunk_size
            end = min(start + chunk_size, n_total)
            indices = list(range(start, end))
            if self.shuffle:
                import random
                random.shuffle(indices)
            yield from indices


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
        wpm = float(info.get('wpm', 0) or 0)

        return (
            torch.tensor(spec).unsqueeze(0),  # (1, T, F)
            torch.tensor(target, dtype=torch.long),
            len(target),
            wpm,
        )


def collate_fn(batch):
    """Custom collate: pad spectrograms and targets to batch max."""
    specs, targets, target_lens, wpms = zip(*batch)
    specs = torch.stack(specs)  # all same size due to pad/truncate
    # Concatenate targets for CTC loss
    targets_cat = torch.cat(targets)
    target_lens = torch.tensor(target_lens, dtype=torch.long)
    wpm_tensor = torch.tensor(wpms, dtype=torch.float32)
    return specs, targets_cat, target_lens, wpm_tensor


class CWDecoder(nn.Module):
    """CNN+BiGRU with CTC output for CW text decoding.

    Also outputs an auxiliary WPM estimate (regression head on mean-pooled
    BiGRU output). At inference time, feed wpm_pred to bmorse -spd so it
    starts the Bayesian trellis at the right speed.
    """

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

        # Auxiliary WPM regression head: mean-pool BiGRU output → scalar WPM
        self.wpm_head = nn.Linear(512, 1)

    def forward(self, x):
        # x: (batch, 1, T, F)
        features = self.cnn(x)  # (batch, 128, T', 4)
        batch_size, channels, time_steps, freq = features.shape

        # Reshape: (batch, T', channels*freq)
        features = features.permute(0, 2, 1, 3).reshape(batch_size, time_steps, channels * freq)

        # RNN
        rnn_out, _ = self.rnn(features)  # (batch, T', 512)

        # Per-timestep character probabilities (CTC)
        ctc_output = self.fc(rnn_out)  # (batch, T', NUM_CLASSES)

        # WPM estimate: global mean pool over time → scalar per sample
        wpm_pred = self.wpm_head(rnn_out.mean(dim=1)).squeeze(-1)  # (batch,)

        return ctc_output, wpm_pred


def train(data_dir='training_data', epochs=50, batch_size=16, resume=False, lr=0.001):
    """Train the CW decoder with CTC loss."""
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}", file=sys.stderr)

    # Try precomputed float16 chunks first
    targets_path = os.path.join(data_dir, 'targets.pt')
    chunk0_path = os.path.join(data_dir, 'specs_chunk_0.pt')
    if os.path.exists(chunk0_path) and os.path.exists(targets_path):
        print(f"Loading precomputed float16 spectrograms (lazy)...", file=sys.stderr)
        data = torch.load(targets_path, weights_only=True)
        n_chunks = data['n_chunks']
        chunk_paths = []
        total_bytes = 0
        for ci in range(n_chunks):
            cp = os.path.join(data_dir, f'specs_chunk_{ci}.pt')
            chunk_paths.append(cp)
            total_bytes += os.path.getsize(cp)
            print(f"  Registered chunk {ci}: {cp} ({os.path.getsize(cp)//1024//1024} MB)", file=sys.stderr)
        chunk_size = data.get('n_samples', 10000) // n_chunks  # samples per chunk
        # Load first chunk to confirm chunk_size
        c0 = torch.load(chunk_paths[0], weights_only=True)
        chunk_size = c0.size(0)
        del c0
        wpm_data = data.get('wpm')
        dataset = ChunkedF16Dataset(chunk_paths, data['targets'], data['target_lens'], chunk_size, wpm=wpm_data)
        wpm_pct = (wpm_data > 0).float().mean().item() * 100 if wpm_data is not None else 0
        print(f"  {data['n_samples']} samples in {n_chunks} chunks ({total_bytes/1e9:.1f} GB float16), {wpm_pct:.0f}% have WPM labels", file=sys.stderr)
    else:
        print("Loading training data from WAVs (slow — run --precompute first)...", file=sys.stderr)
        dataset = CWDataset(data_dir)

    n_total = len(dataset)
    n_train = int(0.85 * n_total)
    n_val = n_total - n_train

    if hasattr(dataset, 'chunk_paths'):
        # Chunk-level train/val split — avoids loading all 18GB into 7.7GB RAM.
        # Training loop iterates chunk files one at a time (load → train → release).
        n_chunks = len(dataset.chunk_paths)
        n_train_chunks = max(1, round(n_chunks * 0.85))
        train_chunk_paths = dataset.chunk_paths[:n_train_chunks]
        val_chunk_paths = dataset.chunk_paths[n_train_chunks:]
        targets = dataset.targets
        target_lens = dataset.target_lens
        wpm_data = dataset.wpm
        chunk_size = dataset.chunk_size
        n_train = n_train_chunks * chunk_size
        n_val = n_total - n_train
        train_loader = None   # replaced by per-chunk loop below
        val_loader = None
        _chunk_mode = True
    else:
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
        _chunk_mode = False

    model = CWDecoder().to(device)
    start_epoch = 0

    # Resume from checkpoint
    if resume and os.path.exists('cw_decoder_ctc.pth'):
        ckpt = torch.load('cw_decoder_ctc.pth', map_location=device, weights_only=True)
        if isinstance(ckpt, dict) and 'model_state_dict' in ckpt:
            missing, _ = model.load_state_dict(ckpt['model_state_dict'], strict=False)
            start_epoch = ckpt.get('epoch', 0)
            if missing:
                print(f"Resumed from epoch {start_epoch} (new weights: {missing})", file=sys.stderr)
            else:
                print(f"Resumed from epoch {start_epoch}", file=sys.stderr)
        else:
            model.load_state_dict(ckpt, strict=False)
            print("Loaded weights (no epoch info)", file=sys.stderr)

    ctc_loss = nn.CTCLoss(blank=BLANK_IDX, zero_infinity=True)
    wpm_loss_fn = nn.MSELoss()
    WPM_LOSS_WEIGHT = 0.01  # WPM auxiliary loss weight — small so CTC dominates
    optimizer = optim.Adam(model.parameters(), lr=lr)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5, factor=0.5)
    scaler = torch.cuda.amp.GradScaler(enabled=(device.type == 'cuda'))
    torch.backends.cudnn.benchmark = True

    param_count = sum(p.numel() for p in model.parameters())
    print(f"Model: {param_count:,} parameters", file=sys.stderr)
    print(f"Training: {n_train} train, {n_val} val, {epochs} epochs, batch {batch_size}", file=sys.stderr)
    print(f"CTC output time steps: 192 (768 input / 4 from time pooling)", file=sys.stderr)

    best_val_loss = float('inf')

    def _make_chunk_loader(chunk_path, chunk_idx_offset, shuffle):
        """Load one chunk from disk and return a DataLoader over it."""
        chunk_specs = torch.load(chunk_path, weights_only=True).float()  # (N, 768, 64)
        n = chunk_specs.size(0)
        start = chunk_idx_offset
        end = start + n
        c_targets = targets[start:end]       # (N, max_target_len)
        c_lens = target_lens[start:end]      # (N,)
        c_wpm = wpm_data[start:end] if wpm_data is not None else torch.zeros(n)

        class _ChunkDataset(torch.utils.data.Dataset):
            def __len__(self): return n
            def __getitem__(self, i):
                spec = chunk_specs[i].float().unsqueeze(0)  # (1, 768, 64)
                tlen = c_lens[i].item()
                return spec, c_targets[i, :tlen], tlen, c_wpm[i].item()

        loader = DataLoader(_ChunkDataset(), batch_size=batch_size, shuffle=shuffle,
                            num_workers=0, collate_fn=collate_fn)
        return loader, chunk_specs

    for epoch in range(start_epoch, start_epoch + epochs):
        # --- Training ---
        print(f"\nEpoch {epoch+1}/{start_epoch+epochs} — training {len(train_chunk_paths)} chunks...",
              file=sys.stderr, flush=True)
        model.train()
        train_loss = 0
        n_batches = 0

        if _chunk_mode:
            import random, gc
            chunk_order = list(range(len(train_chunk_paths)))
            random.shuffle(chunk_order)
            offset = 0
            for chunk_num, ci in enumerate(chunk_order):
                loader, chunk_tensor = _make_chunk_loader(
                    train_chunk_paths[ci], ci * chunk_size, shuffle=True)
                print(f"  train chunk {chunk_num+1}/{len(train_chunk_paths)} ({chunk_tensor.shape[0]} samples)",
                      file=sys.stderr, flush=True)
                for specs, targets_cat, target_lens_batch, wpm_targets in loader:
                    specs = specs.to(device)
                    wpm_targets = wpm_targets.to(device)
                    optimizer.zero_grad()
                    with torch.cuda.amp.autocast(enabled=(device.type == 'cuda')):
                        ctc_output, wpm_pred = model(specs)
                    log_probs = ctc_output.float().permute(1, 0, 2).log_softmax(dim=2)
                    T = log_probs.size(0)
                    B = specs.size(0)
                    input_lengths = torch.full((B,), T, dtype=torch.long)
                    loss = ctc_loss(log_probs, targets_cat.to(device),
                                   input_lengths.to(device), target_lens_batch.to(device))
                    wpm_mask = wpm_targets > 0
                    if wpm_mask.any():
                        wpm_loss = wpm_loss_fn(wpm_pred[wpm_mask].float(), wpm_targets[wpm_mask])
                        if torch.isfinite(wpm_loss):
                            loss = loss + WPM_LOSS_WEIGHT * wpm_loss
                    if torch.isfinite(loss):
                        scaler.scale(loss).backward()
                        scaler.unscale_(optimizer)
                        nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                        scaler.step(optimizer)
                        scaler.update()
                        train_loss += loss.item()
                    n_batches += 1
                del chunk_tensor, loader
                gc.collect()
        else:
            for specs, targets_cat, target_lens, wpm_targets in train_loader:
                specs = specs.to(device)
                wpm_targets = wpm_targets.to(device)
                optimizer.zero_grad()
                ctc_output, wpm_pred = model(specs)
                log_probs = ctc_output.permute(1, 0, 2).log_softmax(dim=2)
                T = log_probs.size(0)
                batch_size_actual = specs.size(0)
                input_lengths = torch.full((batch_size_actual,), T, dtype=torch.long)
                loss = ctc_loss(log_probs, targets_cat.to(device), input_lengths.to(device), target_lens.to(device))
                wpm_mask = wpm_targets > 0
                if wpm_mask.any():
                    wpm_loss = wpm_loss_fn(wpm_pred[wpm_mask], wpm_targets[wpm_mask])
                    if torch.isfinite(wpm_loss):
                        loss = loss + WPM_LOSS_WEIGHT * wpm_loss
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

        wpm_abs_err = 0.0
        wpm_count = 0

        def _val_on_loader(loader):
            nonlocal val_loss, val_batches, char_correct, char_total
            nonlocal exact_match, total_samples, wpm_abs_err, wpm_count
            with torch.no_grad():
                for specs, targets_cat, target_lens_v, wpm_targets in loader:
                    specs = specs.to(device)
                    wpm_targets = wpm_targets.to(device)
                    with torch.cuda.amp.autocast(enabled=(device.type == 'cuda')):
                        ctc_output, wpm_pred = model(specs)
                    ctc_output = ctc_output.float()
                    log_probs = ctc_output.permute(1, 0, 2).log_softmax(dim=2)
                    T = log_probs.size(0)
                    batch_size_actual = specs.size(0)
                    input_lengths = torch.full((batch_size_actual,), T, dtype=torch.long)
                    loss = ctc_loss(log_probs, targets_cat.to(device),
                                   input_lengths.to(device), target_lens_v.to(device))
                    if torch.isfinite(loss):
                        val_loss += loss.item()
                    val_batches += 1
                    wpm_mask = wpm_targets > 0
                    if wpm_mask.any():
                        wpm_abs_err += (wpm_pred[wpm_mask] - wpm_targets[wpm_mask]).abs().sum().item()
                        wpm_count += wpm_mask.sum().item()
                    offset = 0
                    for b in range(batch_size_actual):
                        pred_text = ctc_greedy_decode(ctc_output[b].cpu())
                        tlen = target_lens_v[b].item()
                        true_indices = targets_cat[offset:offset + tlen].tolist()
                        true_text = ''.join(IDX_TO_CHAR[i] for i in true_indices)
                        offset += tlen
                        min_len = min(len(pred_text), len(true_text))
                        for ci in range(min_len):
                            if pred_text[ci] == true_text[ci]:
                                char_correct += 1
                        char_total += max(len(true_text), 1)
                        if pred_text.strip() == true_text.strip():
                            exact_match += 1
                        total_samples += 1

        if _chunk_mode:
            import gc
            n_val_chunks = len(val_chunk_paths)
            for vi, vpath in enumerate(val_chunk_paths):
                vloader, vchunk = _make_chunk_loader(
                    vpath, (n_train_chunks + vi) * chunk_size, shuffle=False)
                _val_on_loader(vloader)
                del vchunk, vloader
                gc.collect()
        else:
            with torch.no_grad():
                for specs, targets_cat, target_lens_v, wpm_targets in val_loader:
                    specs = specs.to(device)
                    wpm_targets = wpm_targets.to(device)
                    ctc_output, wpm_pred = model(specs)
                    log_probs = ctc_output.permute(1, 0, 2).log_softmax(dim=2)
                    T = log_probs.size(0)
                    batch_size_actual = specs.size(0)
                    input_lengths = torch.full((batch_size_actual,), T, dtype=torch.long)
                    loss = ctc_loss(log_probs, targets_cat.to(device),
                                   input_lengths.to(device), target_lens_v.to(device))
                    if torch.isfinite(loss):
                        val_loss += loss.item()
                    val_batches += 1
                    wpm_mask = wpm_targets > 0
                    if wpm_mask.any():
                        wpm_abs_err += (wpm_pred[wpm_mask] - wpm_targets[wpm_mask]).abs().sum().item()
                        wpm_count += wpm_mask.sum().item()
                    offset = 0
                    for b in range(batch_size_actual):
                        pred_text = ctc_greedy_decode(ctc_output[b].cpu())
                        tlen = target_lens_v[b].item()
                        true_indices = targets_cat[offset:offset + tlen].tolist()
                        true_text = ''.join(IDX_TO_CHAR[i] for i in true_indices)
                        offset += tlen
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
        wpm_mae = wpm_abs_err / max(wpm_count, 1)

        scheduler.step(avg_val_loss)

        # Print progress
        if (epoch + 1) % 2 == 0 or epoch == start_epoch:
            # Show a few sample predictions
            model.eval()
            with torch.no_grad():
                if _chunk_mode:
                    _sl, _vc = _make_chunk_loader(val_chunk_paths[0],
                                                  n_train_chunks * chunk_size, shuffle=False)
                    sample_specs, sample_targets, sample_lens, sample_wpms = next(iter(_sl))
                    del _vc, _sl
                else:
                    sample_specs, sample_targets, sample_lens, sample_wpms = next(iter(val_loader))
                sample_ctc, sample_wpm_pred = model(sample_specs.to(device))
                offset = 0
                examples = []
                for b in range(min(3, sample_specs.size(0))):
                    pred = ctc_greedy_decode(sample_ctc[b].cpu())
                    tlen = sample_lens[b].item()
                    true_idx = sample_targets[offset:offset + tlen].tolist()
                    true = ''.join(IDX_TO_CHAR[i] for i in true_idx)
                    offset += tlen
                    wpm_str = f" [{sample_wpm_pred[b].item():.0f} WPM]" if sample_wpms[b] > 0 else ""
                    examples.append(f'    "{true}" → "{pred}"{wpm_str}')

            wpm_str = f" wpm_mae={wpm_mae:.1f}" if wpm_count > 0 else ""
            print(f"Epoch {epoch+1}: train_loss={avg_train_loss:.4f} val_loss={avg_val_loss:.4f} "
                  f"char_acc={char_acc:.1f}% exact={exact_acc:.1f}%{wpm_str} lr={optimizer.param_groups[0]['lr']:.6f}",
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
                'wpm_mae': wpm_mae,
            }, save_path)
            if save_path.endswith('best.pth'):
                print(f"  New best model saved (val_loss={avg_val_loss:.4f})", file=sys.stderr)

    # Final save
    torch.save({
        'model_state_dict': model.state_dict(),
        'epoch': epoch + 1,
        'val_loss': avg_val_loss,
        'char_acc': char_acc,
        'wpm_mae': wpm_mae,
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
    parser.add_argument('--data-dir', default='training_data', help='Training data directory')
    args = parser.parse_args()

    if args.precompute:
        precompute_spectrograms(data_dir=args.data_dir, output_dir=args.data_dir)
    else:
        train(data_dir=args.data_dir, epochs=args.epochs, batch_size=args.batch_size,
              resume=args.resume, lr=args.lr)
