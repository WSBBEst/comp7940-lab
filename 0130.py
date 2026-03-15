# Find all the factors of x using a loop and the operator %
# % means find remainder, for example 10 % 2 = 0; 10 % 3 = 1
# x = 52633
# for i in range(2, x):
#     if x%i == 0:
#         print(i)

# Write a function that prints all factors of the given parameter x
def print_factor(x):
    factor_list = []
    if isinstance(x,int):
        if x == 0 or x == 1:
            factor_list.append(x)
        elif x >= 2:
            for i in range(2, x):
                if x % i == 0:
                    factor_list.append(i)

    return factor_list
# print(print_factor(28888))

# Write a program to find all factors of the numbers in the list l
l = [52633, 8137, 1024, 999]
def print_factors(l):
    for x in l:
        print("the factor of {} is {}".format(x,print_factor(x)))

print_factors(l)
