import json
import sys


def validate(value):
    errors = []
    exit_code = value.get("exit_code")
    if isinstance(exit_code, bool) or exit_code != 0:
        errors.append({"code": "ssh_exit_nonzero", "exit_code": exit_code})
    if value.get("timed_out") is not False:
        errors.append({"code": "ssh_command_timed_out"})
    return {"result": "fail" if errors else "pass", "errors": errors}


if __name__ == "__main__":
    print(json.dumps(validate(json.load(sys.stdin))))
