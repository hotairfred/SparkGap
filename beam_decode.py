#!/usr/bin/env python3
"""
beam_decode.py — CTC beam search decoder with MASTER.SCP trie constraint.

Replaces ctc_greedy_decode with a beam search that uses a callsign database
as a language model. Only rewards COMPLETED valid callsigns at word boundaries
— no per-character prefix noise that causes stuttering.
"""

import os
import re
import numpy as np
from collections import defaultdict

from train_model import BLANK_IDX, IDX_TO_CHAR, NUM_CLASSES

CALLSIGN_RE = re.compile(r'[A-Z0-9]{1,2}\d{1,2}[A-Z]{1,3}(?:/[A-Z0-9]+)?')


class TrieNode:
    __slots__ = ('children', 'is_end')

    def __init__(self):
        self.children = {}
        self.is_end = False


class CallsignTrie:
    """Prefix trie for fast callsign lookup and prefix-constrained search."""

    def __init__(self):
        self.root = TrieNode()
        self.size = 0
        self._words = set()

    def insert(self, word):
        node = self.root
        for c in word:
            if c not in node.children:
                node.children[c] = TrieNode()
            node = node.children[c]
        if not node.is_end:
            node.is_end = True
            self.size += 1
            self._words.add(word)

    def get_node(self, prefix):
        """Return the trie node after following prefix, or None."""
        node = self.root
        for c in prefix:
            if c not in node.children:
                return None
            node = node.children[c]
        return node

    def has_prefix(self, prefix):
        return self.get_node(prefix) is not None

    def is_word(self, word):
        return word in self._words

    def valid_next_chars(self, prefix):
        """Return set of characters that continue a valid prefix."""
        node = self.get_node(prefix)
        if node is None:
            return set()
        return set(node.children.keys())

    @classmethod
    def from_file(cls, path='MASTER.SCP'):
        """Build trie from MASTER.SCP file."""
        trie = cls()
        if not os.path.exists(path):
            print(f"Warning: {path} not found")
            return trie
        with open(path) as f:
            for line in f:
                line = line.strip().upper()
                if line and not line.startswith('#'):
                    trie.insert(line)
        return trie


def _last_word(text):
    """Extract the last word (after last space) from text."""
    idx = text.rfind(' ')
    if idx == -1:
        return text
    return text[idx + 1:]


def ctc_beam_search(log_probs, trie, beam_width=50, lm_weight=2.0,
                    callsign_bonus=5.0):
    """
    CTC prefix beam search with callsign completion reward.

    Only gives LM bonus when a complete callsign from the trie is found
    at a word boundary. No per-character prefix scoring — this keeps the
    search clean and avoids stuttering artifacts.

    Args:
        log_probs: (T, C) raw logits from model output[b]
        trie: CallsignTrie instance
        beam_width: beams to keep per step
        lm_weight: multiplier for LM score when ranking beams
        callsign_bonus: score added when a valid callsign completes

    Returns:
        Best decoded string
    """
    if hasattr(log_probs, 'numpy'):
        log_probs = log_probs.detach().numpy()

    T, C = log_probs.shape

    # Log-softmax
    log_probs = log_probs - np.logaddexp.reduce(log_probs, axis=1, keepdims=True)

    NEG_INF = -float('inf')

    # beam: prefix -> (p_blank, p_nonblank, lm_score)
    beams = {'': (0.0, NEG_INF, 0.0)}

    for t in range(T):
        new_beams = defaultdict(lambda: (NEG_INF, NEG_INF, 0.0))

        # Prune to top beam_width
        if len(beams) > beam_width:
            scored = [(np.logaddexp(pb, pnb) + lm * lm_weight, pfx)
                      for pfx, (pb, pnb, lm) in beams.items()]
            scored.sort(reverse=True)
            beams = {pfx: beams[pfx] for _, pfx in scored[:beam_width]}

        for prefix, (p_b, p_nb, lm_score) in beams.items():
            p_total = np.logaddexp(p_b, p_nb)

            # Blank extension
            p_blank_new = log_probs[t, BLANK_IDX] + p_total
            old = new_beams[prefix]
            new_beams[prefix] = (
                np.logaddexp(old[0], p_blank_new),
                old[1],
                max(old[2], lm_score),
            )

            # Character extensions — only expand top-K most probable chars
            # for speed (skip very unlikely characters)
            top_k = min(10, C - 1)
            top_indices = np.argpartition(log_probs[t, 1:], -top_k)[-top_k:] + 1

            for c_idx in top_indices:
                c = IDX_TO_CHAR[c_idx]
                p_c = log_probs[t, c_idx]

                if prefix and c == prefix[-1]:
                    p_new = p_c + p_b  # repeat char: only via blank
                else:
                    p_new = p_c + p_total

                if p_new <= NEG_INF + 100:
                    continue

                new_prefix = prefix + c
                new_lm = lm_score

                # Callsign completion check at word boundaries
                if c == ' ':
                    last_word = _last_word(prefix)
                    if last_word and len(last_word) >= 3 and trie.is_word(last_word):
                        new_lm += callsign_bonus

                old = new_beams[new_prefix]
                new_beams[new_prefix] = (
                    old[0],
                    np.logaddexp(old[1], p_new),
                    max(old[2], new_lm),
                )

        beams = dict(new_beams)

    # Final: check last word for callsign
    best_score = NEG_INF
    best_prefix = ''
    for prefix, (pb, pnb, lm) in beams.items():
        last_word = _last_word(prefix)
        final_lm = lm
        if last_word and len(last_word) >= 3 and trie.is_word(last_word):
            final_lm += callsign_bonus
        score = np.logaddexp(pb, pnb) + final_lm * lm_weight
        if score > best_score:
            best_score = score
            best_prefix = prefix

    return best_prefix


def ctc_beam_search_constrained(log_probs, trie, beam_width=50, lm_weight=2.0,
                                 callsign_bonus=5.0, prefix_bonus=0.3):
    """
    Trie-CONSTRAINED beam search: during callsign-shaped words, only allow
    characters that continue valid trie prefixes. Falls back to unconstrained
    for non-callsign words (CQ, TEST, 5NN, etc.).

    A word is "callsign-shaped" if its first 2+ chars match a trie prefix
    AND contain a digit.
    """
    if hasattr(log_probs, 'numpy'):
        log_probs = log_probs.detach().numpy()

    T, C = log_probs.shape
    log_probs = log_probs - np.logaddexp.reduce(log_probs, axis=1, keepdims=True)
    NEG_INF = -float('inf')

    beams = {'': (0.0, NEG_INF, 0.0)}

    for t in range(T):
        new_beams = defaultdict(lambda: (NEG_INF, NEG_INF, 0.0))

        if len(beams) > beam_width:
            scored = [(np.logaddexp(pb, pnb) + lm * lm_weight, pfx)
                      for pfx, (pb, pnb, lm) in beams.items()]
            scored.sort(reverse=True)
            beams = {pfx: beams[pfx] for _, pfx in scored[:beam_width]}

        for prefix, (p_b, p_nb, lm_score) in beams.items():
            p_total = np.logaddexp(p_b, p_nb)

            # Blank extension
            p_blank_new = log_probs[t, BLANK_IDX] + p_total
            old = new_beams[prefix]
            new_beams[prefix] = (
                np.logaddexp(old[0], p_blank_new),
                old[1],
                max(old[2], lm_score),
            )

            # Determine current word and whether we're in callsign mode
            current_word = _last_word(prefix)
            in_callsign = (len(current_word) >= 2
                           and any(c.isdigit() for c in current_word)
                           and trie.has_prefix(current_word))
            valid_next = trie.valid_next_chars(current_word) if in_callsign else None

            for c_idx in range(1, C):
                c = IDX_TO_CHAR[c_idx]
                p_c = log_probs[t, c_idx]

                if prefix and c == prefix[-1]:
                    p_new = p_c + p_b
                else:
                    p_new = p_c + p_total

                if p_new <= NEG_INF + 100:
                    continue

                new_prefix = prefix + c
                new_lm = lm_score

                if c == ' ':
                    # Word boundary — check completed callsign
                    if current_word and len(current_word) >= 3 and trie.is_word(current_word):
                        new_lm += callsign_bonus
                elif in_callsign and valid_next is not None and c != ' ':
                    # In callsign mode — boost valid continuations
                    if c in valid_next:
                        new_lm += prefix_bonus
                        # Extra boost for completing a callsign
                        new_word = current_word + c
                        if trie.is_word(new_word):
                            new_lm += prefix_bonus * 2

                old = new_beams[new_prefix]
                new_beams[new_prefix] = (
                    old[0],
                    np.logaddexp(old[1], p_new),
                    max(old[2], new_lm),
                )

        beams = dict(new_beams)

    best_score = NEG_INF
    best_prefix = ''
    for prefix, (pb, pnb, lm) in beams.items():
        last_word = _last_word(prefix)
        final_lm = lm
        if last_word and len(last_word) >= 3 and trie.is_word(last_word):
            final_lm += callsign_bonus
        score = np.logaddexp(pb, pnb) + final_lm * lm_weight
        if score > best_score:
            best_score = score
            best_prefix = prefix

    return best_prefix


def ctc_beam_search_nbest(log_probs, trie, beam_width=50, n_best=10,
                           lm_weight=2.0, callsign_bonus=5.0):
    """Return top N decoded strings with scores."""
    if hasattr(log_probs, 'numpy'):
        log_probs = log_probs.detach().numpy()

    T, C = log_probs.shape
    log_probs = log_probs - np.logaddexp.reduce(log_probs, axis=1, keepdims=True)
    NEG_INF = -float('inf')

    beams = {'': (0.0, NEG_INF, 0.0)}

    for t in range(T):
        new_beams = defaultdict(lambda: (NEG_INF, NEG_INF, 0.0))

        if len(beams) > beam_width:
            scored = [(np.logaddexp(pb, pnb) + lm * lm_weight, pfx)
                      for pfx, (pb, pnb, lm) in beams.items()]
            scored.sort(reverse=True)
            beams = {pfx: beams[pfx] for _, pfx in scored[:beam_width]}

        for prefix, (p_b, p_nb, lm_score) in beams.items():
            p_total = np.logaddexp(p_b, p_nb)

            p_blank_new = log_probs[t, BLANK_IDX] + p_total
            old = new_beams[prefix]
            new_beams[prefix] = (
                np.logaddexp(old[0], p_blank_new),
                old[1],
                max(old[2], lm_score),
            )

            for c_idx in range(1, C):
                c = IDX_TO_CHAR[c_idx]
                p_c = log_probs[t, c_idx]

                if prefix and c == prefix[-1]:
                    p_new = p_c + p_b
                else:
                    p_new = p_c + p_total

                if p_new <= NEG_INF + 100:
                    continue

                new_prefix = prefix + c
                new_lm = lm_score

                if c == ' ':
                    last_word = _last_word(prefix)
                    if last_word and len(last_word) >= 3 and trie.is_word(last_word):
                        new_lm += callsign_bonus

                old = new_beams[new_prefix]
                new_beams[new_prefix] = (
                    old[0],
                    np.logaddexp(old[1], p_new),
                    max(old[2], new_lm),
                )

        beams = dict(new_beams)

    results = []
    for prefix, (pb, pnb, lm) in beams.items():
        last_word = _last_word(prefix)
        final_lm = lm
        if last_word and len(last_word) >= 3 and trie.is_word(last_word):
            final_lm += callsign_bonus
        score = np.logaddexp(pb, pnb) + final_lm * lm_weight
        results.append((prefix, score))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:n_best]


if __name__ == '__main__':
    import sys
    import json
    import wave

    scp_path = sys.argv[1] if len(sys.argv) > 1 else 'MASTER.SCP'
    trie = CallsignTrie.from_file(scp_path)
    print(f"Trie built: {trie.size} callsigns")

    test_calls = ['W1AW', 'K5ZD', 'DK3QN', 'NOTACALL', 'CQ', 'N6TV']
    for call in test_calls:
        print(f"  {call:12s}  word={trie.is_word(call)}")

    if not os.path.exists('cw_decoder_ctc_best.pth'):
        sys.exit(0)

    import torch
    from train_model import CWDecoder, ctc_greedy_decode, compute_spectrogram

    print(f"\nComparing greedy vs beam search on validation samples:")
    device = torch.device('cpu')
    model = CWDecoder().to(device)
    ckpt = torch.load('cw_decoder_ctc_best.pth', map_location=device, weights_only=True)
    model.load_state_dict(ckpt['model_state_dict'])
    model.eval()

    with open('training_data/labels.json') as f:
        labels = json.load(f)

    # Stats
    greedy_valid = 0
    beam_valid = 0
    beam_constrained_valid = 0
    greedy_exact = 0
    beam_exact = 0
    n_test = min(50, len(labels))

    for info in labels[:n_test]:
        wav_path = os.path.join('training_data', info['file'])
        w = wave.open(wav_path, 'rb')
        frames = w.readframes(w.getnframes())
        w.close()
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        spec = compute_spectrogram(samples)
        if spec.shape[0] < 512:
            spec = np.pad(spec, ((0, 512 - spec.shape[0]), (0, 0)))
        else:
            spec = spec[:512]

        tensor = torch.tensor(spec).unsqueeze(0).unsqueeze(0)
        with torch.no_grad():
            output = model(tensor)

        greedy = ctc_greedy_decode(output[0].cpu())
        beam = ctc_beam_search(output[0].cpu(), trie)
        beam_c = ctc_beam_search_constrained(output[0].cpu(), trie)

        true_text = info['text']
        true_call = info.get('callsign', '')

        # Count valid callsigns found
        g_calls = set(CALLSIGN_RE.findall(greedy))
        b_calls = set(CALLSIGN_RE.findall(beam))
        bc_calls = set(CALLSIGN_RE.findall(beam_c))
        g_v = {c for c in g_calls if trie.is_word(c)}
        b_v = {c for c in b_calls if trie.is_word(c)}
        bc_v = {c for c in bc_calls if trie.is_word(c)}
        greedy_valid += len(g_v)
        beam_valid += len(b_v)
        beam_constrained_valid += len(bc_v)

        if greedy.strip() == true_text.strip():
            greedy_exact += 1
        if beam.strip() == true_text.strip():
            beam_exact += 1

        # Print interesting differences
        if g_v != b_v or g_v != bc_v:
            print(f"\n  TRUE:       {true_text}")
            print(f"  GREEDY:     {greedy}  valid={g_v}")
            print(f"  BEAM:       {beam}  valid={b_v}")
            print(f"  BEAM+TRIE:  {beam_c}  valid={bc_v}")

    print(f"\n{'='*60}")
    print(f"Results over {n_test} samples:")
    print(f"  Greedy:          {greedy_valid} valid callsigns, {greedy_exact} exact match")
    print(f"  Beam:            {beam_valid} valid callsigns, {beam_exact} exact match")
    print(f"  Beam+Constrain:  {beam_constrained_valid} valid callsigns")
