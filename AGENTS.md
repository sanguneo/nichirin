# AGENTS.md — working on nichirin

Two builds of one KakaoTalk tool, merged from `sanguneo/kanao-cli` (Windows)
and `sanguneo/kamado-cli` (macOS) with both histories kept. Each platform
directory has its own `AGENTS.md`, and the deeper file wins for anything under
it.

## Layout

| Path | What it is |
| --- | --- |
| `openapi.json` | The one HTTP + SSE contract both `serve` commands answer |
| `kakaocommon/` | Modules both builds run unchanged (`wal`, `sixel`, `budget`, `register`: announcing `serve` to saya) |
| `windows/` | `kanao-cli`: package `kanao`, Python 3.10+, its tests, skill and docs |
| `macos/` | `kamado-cli`: package `kamado`, Python 3.9+, its tests, skill and docs |
| `kanao/`, `kamado/`, `kakaoread/`, `install.py` | Root entry points that keep the pre-merge commands working from the root; no code of their own |
| `nichirin/` | `python -m nichirin`: runs `kamado.cli` on a Mac and `kanao.cli` elsewhere, same arguments |

The root `kanao` and `kamado` packages set their `__path__` to the real
package and run its `__init__`, so every module still loads once, from
`windows/` or `macos/`, under its usual name. The root `kakaoread` and
`install.py` pick `macos/` on a Mac and `windows/` elsewhere. Put nothing
else in them. The platform packages append the root to `sys.path` only when
`kakaocommon` is missing, so a suite run inside `windows/` or `macos/` never
picks up these redirects instead of the real package.

## Commands

Each `tests/__init__.py` appends the repository root to `sys.path` when
`kakaocommon` is not importable yet, so any single test module runs on its own;
it appends and never inserts, so the root redirect packages never shadow the
real ones. `install.py` refuses a checkout without `kakaocommon/` rather than
installing a launcher that cannot import. `windows/bench/bench_read_path.py`
measures the read path on synthetic data and asserts nothing.

Run each suite from its own directory; discovery needs `-t .`.

```powershell
cd windows; python -m unittest discover -t . -s tests
```

```bash
cd macos && python3 -m unittest discover -t . -s tests
```

A suite run on the other platform skips what needs that platform; it is not
evidence for it. A change to `kakaocommon/` or `openapi.json` wants both suites,
the Windows one on Windows and the macOS one on a Mac; when only one machine is
at hand, the next section says what to do about the other.

## Working from one platform

Most contributors have only a Mac or only a Windows machine, and that is the
expected case (`CONTRIBUTING.md`). Work and test on the machine you are on.
When the change also reaches the other platform and you cannot run it there:

- Say so in the PR body: which platform was not checked, and what to run.
- Offer to open a short check issue, and open it when the person agrees:
  `gh issue create --template check-windows.md` (or `check-macos.md`), or
  with `--label "check: windows"` / `--label "check: macos"` and three lines:
  what changed (PR or commit), what to run there and what to expect, what
  you ran here and its last line.
- It is a courtesy, not a gate. Do not block, delay or refuse a change
  because the other platform is unchecked, and never present a skipped suite
  as evidence for the other platform.

It reaches the other platform when it touches `kakaocommon/`, `openapi.json`,
the root entry points, or anything both builds must answer the same way
(commands, options, JSON envelope keys, error codes). A change inside one
build's directory with no visible difference needs no issue.

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
- **One GitHub release per shipment, not one per build.** Tag the merge
  commit `nichirin-YYYY.MM.DD` (`.2`, `.3` for a later one the same day),
  title it with each build's version that ships (`nichirin 2026.10.06 — kanao
  0.31.0 · kamado 0.29.0`), and put each changelog's section under its own
  heading. `kanao-vX` / `kamado-vY` stay as plain tags on the same commit.
  Two releases made GitHub mark whichever was published last as Latest, so
  one platform's users were shown the other build.
