#!/usr/bin/env python3
"""
finetune_frozen.py — Fine-tune CW decoder with frozen CNN.

Freezes the CNN backbone (including BatchNorm running stats) and only
updates the BiGRU + FC + WPM head. Prevents catastrophic forgetting
caused by full-network fine-tuning on out-of-distribution real audio.

Usage: python3 finetune_frozen.py [--data-dir training_data_w1aw_mix] [--epochs 10] [--lr 1e-4]
"""

import os
import sys
import argparse

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

from train_model import (
    CWDecoder, ctc_greedy_decode, ChunkedF16Dataset, collate_fn,
    BLANK_IDX, IDX_TO_CHAR, NUM_CLASSES
)


def finetune(data_dir, checkpoint, output, epochs, lr, batch_size):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Device: {device}", file=sys.stderr)

    # Load precomputed chunks
    targets_path = os.path.join(data_dir, 'targets.pt')
    chunk0_path = os.path.join(data_dir, 'specs_chunk_0.pt')
    if not os.path.exists(chunk0_path):
        print(f"ERROR: no precomputed chunks in {data_dir} — run --precompute first", file=sys.stderr)
        sys.exit(1)

    data = torch.load(targets_path, weights_only=True)
    n_chunks = data['n_chunks']
    chunks = []
    for ci in range(n_chunks):
        cp = os.path.join(data_dir, f'specs_chunk_{ci}.pt')
        chunk = torch.load(cp, weights_only=True)
        chunks.append(chunk)
        print(f"  Loaded chunk {ci} {chunk.shape}", file=sys.stderr)
    chunk_size = chunks[0].size(0)
    wpm_data = data.get('wpm')
    dataset = ChunkedF16Dataset(chunks, data['targets'], data['target_lens'], chunk_size, wpm=wpm_data)
    print(f"  {len(dataset)} samples", file=sys.stderr)

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
                            num_workers=0, collate_fn=collate_fn)

    # Load model
    model = CWDecoder().to(device)
    ckpt = torch.load(checkpoint, map_location=device, weights_only=True)
    if isinstance(ckpt, dict) and 'model_state_dict' in ckpt:
        model.load_state_dict(ckpt['model_state_dict'], strict=False)
        start_epoch = ckpt.get('epoch', 0)
        print(f"Loaded checkpoint: epoch {start_epoch}, char_acc={ckpt.get('char_acc', 0):.1f}%", file=sys.stderr)
    else:
        model.load_state_dict(ckpt, strict=False)
        start_epoch = 0

    # Freeze CNN — both weights and BatchNorm running stats
    for param in model.cnn.parameters():
        param.requires_grad = False
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    frozen = sum(p.numel() for p in model.cnn.parameters())
    print(f"Frozen CNN: {frozen:,} params | Trainable (GRU+FC): {trainable:,} params", file=sys.stderr)

    # Optimizer only covers trainable params
    optimizer = optim.Adam([p for p in model.parameters() if p.requires_grad], lr=lr)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=3, factor=0.5)

    ctc_loss_fn = nn.CTCLoss(blank=BLANK_IDX, zero_infinity=True)
    wpm_loss_fn = nn.MSELoss()
    WPM_WEIGHT = 0.01

    best_val_loss = float('inf')

    for epoch in range(start_epoch, start_epoch + epochs):
        # Training — keep CNN in eval mode to freeze BN running stats
        model.train()
        model.cnn.eval()
        train_loss = 0
        n_batches = 0

        for specs, targets_cat, target_lens, wpm_targets in train_loader:
            specs = specs.to(device)
            wpm_targets = wpm_targets.to(device)
            optimizer.zero_grad()

            ctc_output, wpm_pred = model(specs)
            log_probs = ctc_output.permute(1, 0, 2).log_softmax(dim=2)
            T = log_probs.size(0)
            B = specs.size(0)
            input_lengths = torch.full((B,), T, dtype=torch.long)

            loss = ctc_loss_fn(log_probs, targets_cat.to(device),
                               input_lengths.to(device), target_lens.to(device))

            wpm_mask = wpm_targets > 0
            if wpm_mask.any():
                wl = wpm_loss_fn(wpm_pred[wpm_mask], wpm_targets[wpm_mask])
                if torch.isfinite(wl):
                    loss = loss + WPM_WEIGHT * wl

            if torch.isfinite(loss):
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                optimizer.step()
                train_loss += loss.item()
            n_batches += 1

        avg_train = train_loss / max(n_batches, 1)

        # Validation
        model.eval()
        val_loss = 0
        val_batches = 0
        char_correct = char_total = exact_match = total_samples = 0
        wpm_err = wpm_count = 0

        with torch.no_grad():
            for specs, targets_cat, target_lens, wpm_targets in val_loader:
                specs = specs.to(device)
                wpm_targets = wpm_targets.to(device)
                ctc_output, wpm_pred = model(specs)
                log_probs = ctc_output.permute(1, 0, 2).log_softmax(dim=2)
                T = log_probs.size(0)
                B = specs.size(0)
                input_lengths = torch.full((B,), T, dtype=torch.long)
                loss = ctc_loss_fn(log_probs, targets_cat.to(device),
                                   input_lengths.to(device), target_lens.to(device))
                if torch.isfinite(loss):
                    val_loss += loss.item()
                val_batches += 1

                wpm_mask = wpm_targets > 0
                if wpm_mask.any():
                    wpm_err += (wpm_pred[wpm_mask] - wpm_targets[wpm_mask]).abs().sum().item()
                    wpm_count += wpm_mask.sum().item()

                offset = 0
                for b in range(B):
                    pred = ctc_greedy_decode(ctc_output[b].cpu())
                    tlen = target_lens[b].item()
                    true_idx = targets_cat[offset:offset + tlen].tolist()
                    true = ''.join(IDX_TO_CHAR[i] for i in true_idx)
                    offset += tlen
                    min_len = min(len(pred), len(true))
                    for ci in range(min_len):
                        if pred[ci] == true[ci]:
                            char_correct += 1
                    char_total += max(len(true), 1)
                    if pred.strip() == true.strip():
                        exact_match += 1
                    total_samples += 1

        avg_val = val_loss / max(val_batches, 1)
        char_acc = char_correct / max(char_total, 1) * 100
        wpm_mae = wpm_err / max(wpm_count, 1)
        scheduler.step(avg_val)

        # Sample predictions
        with torch.no_grad():
            sample_specs, sample_targets, sample_lens, sample_wpms = next(iter(val_loader))
            sample_ctc, _ = model(sample_specs.to(device))
            offset = 0
            examples = []
            for b in range(min(3, sample_specs.size(0))):
                pred = ctc_greedy_decode(sample_ctc[b].cpu())
                tlen = sample_lens[b].item()
                true_idx = sample_targets[offset:offset + tlen].tolist()
                true = ''.join(IDX_TO_CHAR[i] for i in true_idx)
                offset += tlen
                wpm_str = f" [{sample_wpms[b]:.0f} WPM]" if sample_wpms[b] > 0 else ""
                examples.append(f'    "{true}" → "{pred}"{wpm_str}')

        print(f"Epoch {epoch+1}: train={avg_train:.4f} val={avg_val:.4f} "
              f"char_acc={char_acc:.1f}% wpm_mae={wpm_mae:.1f} "
              f"lr={optimizer.param_groups[0]['lr']:.2e}", file=sys.stderr)
        for ex in examples:
            print(ex, file=sys.stderr)

        # Save
        if avg_val < best_val_loss:
            best_val_loss = avg_val
            torch.save({
                'model_state_dict': model.state_dict(),
                'epoch': epoch + 1,
                'val_loss': avg_val,
                'char_acc': char_acc,
                'wpm_mae': wpm_mae,
            }, output)
            print(f"  Saved {output} (val={avg_val:.4f})", file=sys.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='training_data_w1aw_mix')
    parser.add_argument('--checkpoint', default='cw_decoder_ctc_best.pth')
    parser.add_argument('--output', default='cw_decoder_ctc_frozen.pth')
    parser.add_argument('--epochs', type=int, default=10)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--batch-size', type=int, default=8)
    args = parser.parse_args()

    finetune(args.data_dir, args.checkpoint, args.output,
             args.epochs, args.lr, args.batch_size)
