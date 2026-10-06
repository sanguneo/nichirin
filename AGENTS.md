# AGENTS.md — working on this repository

Two builds of one KakaoTalk tool, merged from `sanguneo/kanao-cli` (Windows)
and `sanguneo/kamado-cli` (macOS) with both histories kept. Each platform
directory has its own `AGENTS.md`, and the deeper file wins for anything under
it.

## Layout

| Path | What it is |
| --- | --- |
| `openapi.json` | The one HTTP + SSE contract both `serve` commands answer |
| `kakaocommon/` | Modules both builds run unchanged (`wal`, `sixel`) |
| `windows/` | `kanao-cli`: package `kanao`, Python 3.10+, its tests, skill and docs |
| `macos/` | `kamado-cli`: package `kamado`, Python 3.9+, its tests, skill and docs |

## Commands

Run each suite from its own directory; discovery needs `-t .`.

```powershell
cd windows; python -m unittest discover -t . -s tests
```

```bash
cd macos && python3 -m unittest discover -t . -s tests
```

A suite run on the other platform skips what needs that platform; it is not
evidence for it. A change to `kakaocommon/` or `openapi.json` runs both suites,
the Windows one on Windows and the macOS one on a Mac.

## Rules for the shared parts

- **`openapi.json` is copied byte for byte by its consumers** (saya pins its
  SHA-256), so `.gitattributes` keeps its line endings untouched. Do not
  reformat it. A change bumps `info.version` and lands with both builds
  answering it, or marks the member `x-platform`.
- **`kakaocommon/` holds only code that is the same for both platforms** and
  imports nothing from `kanao` or `kamado`. Python 3.9 is its floor. A module
  that needs a platform's error class, model or paths stays in the platform
  package until that dependency is passed in instead.
- Each platform package reaches `kakaocommon` itself: an installed skill ships
  it beside the package (`install.py`), and a checkout finds it one level up
  (`_reach_common` in each `__init__.py`). The user-facing names do not change:
  `python -m kanao`, `python3 -m kamado`, the skills `kanao-cli` and
  `kamado-cli`, and their launcher paths.
- Versions and changelogs stay per platform (`windows/CHANGELOG.md`,
  `macos/CHANGELOG.md`).
