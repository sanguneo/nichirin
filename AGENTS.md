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
| `kanao/`, `kamado/`, `kakaoread/`, `install.py` | Root entry points that keep the pre-merge commands working from the root; no code of their own |

The root `kanao` and `kamado` packages set their `__path__` to the real
package and run its `__init__`, so every module still loads once, from
`windows/` or `macos/`, under its usual name. The root `kakaoread` and
`install.py` pick `macos/` on a Mac and `windows/` elsewhere. Put nothing
else in them. The platform packages append the root to `sys.path` only when
`kakaocommon` is missing, so a suite run inside `windows/` or `macos/` never
picks up these redirects instead of the real package.

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
