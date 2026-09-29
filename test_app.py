def add_numbers(a: int, b: int) -> int:
    return a + b


def calculate_total(numbers):
    total = 0

    for number in numbers:
        total += number

    return total


def greet_user(name):
    message = f"Hello, {name}!"
    print(message)


def main():
    numbers = [10, 20, 30]

    total = calculate_total(numbers)
    result = add_numbers(total, 5)

    greet_user("Saksham")

    print(f"Total: {result}")


if __name__ == "__main__":
    main()