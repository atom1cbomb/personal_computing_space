from pathlib import Path
import re

from loom_v2.observer.orchestration_preflight import check_program, validate_entry_signature


async def test_existing_activation_skill_program_satisfies_runtime_type_contract():
    reference = Path(__file__).resolve().parents[2] / "skills/personal-computing-space/references/existing-activations.md"
    program, = re.findall(r"```python\n(.*?)```", reference.read_text(), re.S)
    assert validate_entry_signature(program) == []
    assert await check_program(program) == []
