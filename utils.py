def calculate_average(numbers):
    if not numbers:
        return 0
    total = 0
    for num in numbers:
        total += num
    return total / len(numbers)


def get_user_name(user):
    name = user.get("name")
    if not name:
        return ""
    return name.upper()
