# Loom Business Skills

An independently packaged external-agent plugin for learning reusable business
procedures against Loom's public interfaces. It does not change Loom services,
the generic skill, SDK, or protocol. See [the design](DESIGN.md).

**One entry skill only.** Experience grows in a private SQLite cache, not in the
agent's automatically loaded skill catalog. Search defaults to three short matches;
prepare loads one concise procedure after fresh live checks. Generated business
`SKILL.md` views remain outside automatic skill discovery directories.

## Use the distributable

Extract the ZIP into a local plugin directory. Install its bundled public SDK wheel
in a Python 3.12+ environment:

```sh
python -m pip install /path/to/plugin/assets/wheels/loom_orchestration-0.10.0-py3-none-any.whl
python /path/to/plugin/scripts/memory.py --help
```

Load the directory using the host agent's plugin installation workflow. The root
`plugin.json` follows the [Agent Plugins layout](https://developers.openai.com/plugins/build/plugins)
and declares the [local executor requirement](https://developers.openai.com/plugins/deploy/desktop-only).
Only `skills/loom-business-memory` is discoverable. Other agents can load that one
SKILL.md and use the same CLI; there is no runtime dependency on Codex APIs.
Installation does not modify user-wide agent configuration or install cached skills.

Get the Observer connection, token file, and optional trusted data bindings from
the operator. Then follow [the command reference](references/workflow.md).
The default private cache is `~/.local/share/loom-business-skills`; `--cache` can
select an isolated workspace directory. Remove it to discard all learned experience.
Historical hints never replace current contracts, permission or authentication.

## Develop independently

Install a matching published Loom SDK wheel first (it is not assumed to be on PyPI).
Then this directory can be installed as a standalone Python distribution:

```sh
python -m pip install --no-deps -e .
python -m pytest tests -q
python scripts/build_plugin.py --sdk-wheel /path/to/loom_orchestration-0.10.0-py3-none-any.whl \
  --output /path/to/loom-business-skills.zip
```

The plugin does not store business arguments or raw results. Result files are
separate, private, caller-owned artifacts; experience holds contract metadata,
parameterized instructions, and execution references. Credential-pattern checks
are a guard against common mistakes, not a complete private-data classifier.
Keep cache directories private and review any material selected for sharing.
