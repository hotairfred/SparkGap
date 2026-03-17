#!/usr/bin/env python3
"""
train_model.py — Train a CW callsign extraction model.

Uses synthetic CW training data to train a CNN model that
extracts callsigns from spectrograms.

Architecture: Spectrogram → CNN → classify as callsign or not
Simpler than full CTC decoding — just detect if a callsign is present.
"""

import json
import os
import sys
import wave
import struct
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader


class CWSpecDataset(Dataset):
    """Dataset of CW spectrograms with callsign labels."""

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

        # Read WAV
        w = wave.open(wav_path, 'rb')
        frames = w.readframes(w.getnframes())
        w.close()
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

        # Compute spectrogram
        fft_size = 128
        hop = 32
        n_frames = min((len(samples) - fft_size) // hop + 1, 256)  # cap at 256 frames
        window = np.hanning(fft_size)

        spec = np.zeros((n_frames, fft_size // 2), dtype=np.float32)
        for i in range(n_frames):
            frame = samples[i * hop: i * hop + fft_size] * window
            fft = np.abs(np.fft.rfft(frame))[:-1]
            spec[i] = fft

        spec = np.log1p(spec)

        # Pad or truncate to fixed size
        target_frames = 256
        if n_frames < target_frames:
            spec = np.pad(spec, ((0, target_frames - n_frames), (0, 0)))
        else:
            spec = spec[:target_frames]

        # Label: the callsign (we'll encode as character sequence)
        call = info['call']

        # For binary classification: 1 = has callsign, 0 = noise
        # For now, encode callsign as a hash for multi-class
        # Simple approach: just train to detect CW vs noise
        label = 1.0  # all training data has callsigns

        return torch.tensor(spec).unsqueeze(0), torch.tensor(label)


class CWDetector(nn.Module):
    """Simple CNN for CW signal detection."""

    def __init__(self):
        super().__init__()
        # Input: 1 x 256 x 64 (spectrogram)
        self.features = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),  # 128 x 32
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),  # 64 x 16
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),  # 32 x 8
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((4, 4)),  # 4 x 4
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(128 * 4 * 4, 256),
            nn.ReLU(),
            nn.Dropout(0.5),
            nn.Linear(256, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
            nn.Sigmoid()
        )

    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x)
        return x


class CWCallsignDecoder(nn.Module):
    """CNN+LSTM for decoding callsign characters from spectrogram."""

    # Characters the model can output
    CHARS = ' ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/'
    CHAR_TO_IDX = {c: i for i, c in enumerate(CHARS)}
    IDX_TO_CHAR = {i: c for i, c in enumerate(CHARS)}

    def __init__(self, max_output_len=12):
        super().__init__()
        self.max_output_len = max_output_len

        # CNN feature extractor
        self.features = nn.Sequential(
            nn.Conv2d(1, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d((2, 1)),  # reduce time, keep freq
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d((2, 1)),
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d((2, 1)),
        )

        # LSTM for sequence decoding
        self.lstm = nn.LSTM(
            input_size=128 * 64,  # channels * freq_bins after pooling
            hidden_size=256,
            num_layers=2,
            batch_first=True,
            dropout=0.3,
            bidirectional=True,
        )

        # Output character probabilities
        self.fc = nn.Linear(512, len(self.CHARS))  # 512 = 256 * 2 (bidirectional)

    def forward(self, x):
        # x: (batch, 1, time, freq)
        batch_size = x.size(0)
        features = self.features(x)  # (batch, 128, time/8, freq)

        # Reshape for LSTM: (batch, time_steps, features)
        t = features.size(2)
        features = features.permute(0, 2, 1, 3).reshape(batch_size, t, -1)

        # LSTM
        lstm_out, _ = self.lstm(features)

        # Take output at each time step
        output = self.fc(lstm_out)  # (batch, time_steps, n_chars)

        return output


def encode_callsign(call, max_len=12):
    """Encode a callsign string to tensor of character indices."""
    chars = CWCallsignDecoder.CHARS
    indices = []
    for c in call.upper()[:max_len]:
        if c in CWCallsignDecoder.CHAR_TO_IDX:
            indices.append(CWCallsignDecoder.CHAR_TO_IDX[c])
        else:
            indices.append(0)  # space for unknown
    # Pad with spaces
    while len(indices) < max_len:
        indices.append(0)
    return torch.tensor(indices, dtype=torch.long)


def decode_output(output_tensor):
    """Decode model output to callsign string."""
    indices = output_tensor.argmax(dim=-1)
    chars = []
    for idx in indices:
        c = CWCallsignDecoder.IDX_TO_CHAR.get(idx.item(), ' ')
        chars.append(c)
    return ''.join(chars).strip()


def train_detector(data_dir='training_data', epochs=20, batch_size=32):
    """Train the CW detector model."""
    print("Loading training data...", file=sys.stderr)
    dataset = CWSpecDataset(data_dir, max_samples=4000)
    train_size = int(0.8 * len(dataset))
    val_size = len(dataset) - train_size
    train_set, val_set = torch.utils.data.random_split(dataset, [train_size, val_size])

    train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_set, batch_size=batch_size, num_workers=0)

    model = CWDetector()
    criterion = nn.BCELoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001)

    print(f"Training CW detector: {len(train_set)} train, {len(val_set)} val", file=sys.stderr)

    for epoch in range(epochs):
        model.train()
        train_loss = 0
        for batch_spec, batch_label in train_loader:
            optimizer.zero_grad()
            output = model(batch_spec).squeeze()
            loss = criterion(output, batch_label)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()

        # Validation
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch_spec, batch_label in val_loader:
                output = model(batch_spec).squeeze()
                loss = criterion(output, batch_label)
                val_loss += loss.item()

        if (epoch + 1) % 5 == 0 or epoch == 0:
            print(f"  Epoch {epoch+1}/{epochs}: train_loss={train_loss/len(train_loader):.4f} val_loss={val_loss/len(val_loader):.4f}", file=sys.stderr)

    # Save model
    torch.save(model.state_dict(), 'cw_detector.pth')
    print("Model saved to cw_detector.pth", file=sys.stderr)
    return model


def train_decoder(data_dir='training_data', epochs=30, batch_size=16):
    """Train the CW callsign decoder model."""
    print("Loading training data for decoder...", file=sys.stderr)

    # Load labels
    with open(os.path.join(data_dir, 'labels.json')) as f:
        all_labels = json.load(f)[:4000]

    # Process data
    spectrograms = []
    targets = []

    for info in all_labels:
        wav_path = os.path.join(data_dir, info['file'])
        w = wave.open(wav_path, 'rb')
        frames = w.readframes(w.getnframes())
        w.close()
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

        # Spectrogram
        fft_size = 128
        hop = 32
        n_frames = min((len(samples) - fft_size) // hop + 1, 256)
        window = np.hanning(fft_size)
        spec = np.zeros((256, fft_size // 2), dtype=np.float32)
        for i in range(n_frames):
            frame = samples[i * hop: i * hop + fft_size] * window
            spec[i] = np.abs(np.fft.rfft(frame))[:-1]
        spec = np.log1p(spec)

        spectrograms.append(torch.tensor(spec).unsqueeze(0))
        targets.append(encode_callsign(info['call']))

    print(f"Processed {len(spectrograms)} samples", file=sys.stderr)

    # Create model
    model = CWCallsignDecoder()
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.5)

    # Training loop
    n_train = int(0.8 * len(spectrograms))
    print(f"Training callsign decoder: {n_train} train, {len(spectrograms)-n_train} val", file=sys.stderr)

    for epoch in range(epochs):
        model.train()
        total_loss = 0
        correct = 0
        total = 0

        # Mini-batch training
        indices = np.random.permutation(n_train)
        for i in range(0, n_train, batch_size):
            batch_idx = indices[i:i+batch_size]
            batch_specs = torch.stack([spectrograms[j] for j in batch_idx])
            batch_targets = torch.stack([targets[j] for j in batch_idx])

            optimizer.zero_grad()
            output = model(batch_specs)  # (batch, time_steps, n_chars)

            # We need to match output time steps to target length
            # Take the first max_output_len time steps
            output_trimmed = output[:, :12, :]  # (batch, 12, n_chars)

            # Compute loss across all character positions
            loss = 0
            for pos in range(12):
                loss += criterion(output_trimmed[:, pos, :], batch_targets[:, pos])
            loss /= 12

            loss.backward()
            optimizer.step()
            total_loss += loss.item()

            # Accuracy: check if predicted callsign matches
            for b in range(len(batch_idx)):
                pred = decode_output(output_trimmed[b])
                true = ''.join([CWCallsignDecoder.IDX_TO_CHAR.get(t.item(), ' ') for t in batch_targets[b]]).strip()
                if pred.strip()[:len(true)] == true:
                    correct += 1
                total += 1

        scheduler.step()

        # Validation
        model.eval()
        val_correct = 0
        val_total = 0
        with torch.no_grad():
            for j in range(n_train, len(spectrograms)):
                spec = spectrograms[j].unsqueeze(0)
                target = targets[j]
                output = model(spec)
                output_trimmed = output[:, :12, :]
                pred = decode_output(output_trimmed[0])
                true = ''.join([CWCallsignDecoder.IDX_TO_CHAR.get(t.item(), ' ') for t in target]).strip()
                if pred.strip()[:len(true)] == true:
                    val_correct += 1
                val_total += 1

        if (epoch + 1) % 5 == 0 or epoch == 0:
            train_acc = correct / max(total, 1) * 100
            val_acc = val_correct / max(val_total, 1) * 100
            print(f"  Epoch {epoch+1}/{epochs}: loss={total_loss*batch_size/n_train:.4f} train_acc={train_acc:.1f}% val_acc={val_acc:.1f}%", file=sys.stderr)

    # Save model
    torch.save(model.state_dict(), 'cw_decoder.pth')
    print(f"Model saved to cw_decoder.pth", file=sys.stderr)
    print(f"Final validation accuracy: {val_correct/max(val_total,1)*100:.1f}%", file=sys.stderr)
    return model


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'detector':
        train_detector()
    else:
        train_decoder()
