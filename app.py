import os


def calculate_sum(numbers):
    unused_variable = 123
    total = 0

    for number in numbers:
        total += number

    return total


def greet(name):
    print("Hello " + name)


result = calculate_sum([1, 2, 3])
print(result)