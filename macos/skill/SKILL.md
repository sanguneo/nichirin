# kamado-cli skill source

The canonical skill is [`kamado/agent_docs/SKILL.md`](../kamado/agent_docs/SKILL.md).
`install.py` copies that file to the installed skill's `SKILL.md` and bundles
the same resource with the runtime. Wheels include it as package data.

From a checkout, run `python3 -m kamado skills get` for the full document, or
`python3 -m kamado skills get send` (also `read`, `serve`) for a topic.
The launchers in `scripts/` and their installed paths are unchanged.
