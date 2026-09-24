Drop sanitized real .txt email exports here alongside the synthetic ones. Swap real
names/addresses before saving.

Name them `real_*.txt` -- that prefix is gitignored, so a real export cannot be
committed by accident. The `synthetic_*.txt` files are invented and are committed on
purpose.

Note that `extract_test.py` sends the contents of every `.txt` in this directory to the
Anthropic API. Sanitizing before saving is the only control on that.

Each run writes `out_<name>.json` beside the source email. Those for `real_*` inputs
are gitignored too -- an extraction of client correspondence is still client
correspondence.
