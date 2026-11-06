# RBN auth library

`rbn2_feeder.py` depends on `librbn2_auth.so`, a small private library that
fills in the two fields RBN's upload scripts check on a feeder. Its interface is
`rbn2_auth.h`; the library itself is not in this repo and is not published. It
is available from Chris WX7V on request, with a `SHA256SUMS.txt` to check it
against. Setup and use: `sending-spots.md`.

## Interface

Three functions, nothing else exported:

- `rbn2_auth_version`: the library's version string (`rbn2auth/1.2`). A client
  compares it with the one it was written for and stops on a mismatch; it
  changes only on a breaking change.
- `rbn2_auth_short_hash`: the sign-in field for `id.php`.
- `rbn2_auth_spot_hash`: the field for one `s.php` post, from the batch's calls
  in row order.

Each hash is 8 hex digits and a NUL. On bad input or a short buffer a function
returns -1 and writes nothing. Linux x86_64.

## How the feeder uses it

Start up:

1. Load `librbn2_auth.so` from the folder that contains the SparkGap checkout,
   never inside it (`auth_lib_path()`). Missing or fails to load: stop, log why,
   post nothing. There is no fallback.
2. Call `rbn2_auth_version` into a 64-byte buffer and compare it with the
   version the feeder takes. Anything else: stop and say which version was
   found.
3. Call `rbn2_auth_short_hash` for the sign-in and post `id.php` with the
   result. The library's version string is for the check in step 2 only and is
   not sent.
4. Repeat the `id.php` post every 300 s.

Run:

1. Spots go into a queue as they are made, after SparkGap's spot rules.
2. On every tick, nothing queued means no post. Otherwise every spot queued
   since the last post, in order, is one batch.
3. Call `rbn2_auth_spot_hash` with the batch's calls in row order. On -1, do not
   post the batch; log it without any hash value.
4. Post `s.php` with the spot rows and the result. The rows and the hashed calls
   are the same list in the same order.
5. A failed post is not retried: its spots are dropped.

No hash value or sign-in string is ever logged.
