import subprocess


def run_pylint(filename):
    result = subprocess.run(
        ["pylint", filename],
        capture_output=True,
        text=True,
    )

    print("===== PYLINT OUTPUT =====")
    print(result.stdout)

    if result.stderr:
        print("===== PYLINT ERRORS =====")
        print(result.stderr)

    print("===== EXIT CODE =====")
    print(result.returncode)


if __name__ == "__main__":
    run_pylint("app.py")