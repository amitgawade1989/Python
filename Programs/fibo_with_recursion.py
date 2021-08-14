def fibonacci(number):
    '''This function calculates the fibonacci series till the n-th term'''
    if number <= 1:
        return number
    else:
        return (fibonacci(number - 1) + fibonacci(number - 2))
userInput = int(input('Enter the number upto which you wish to calculate fibonnaci series: '))
for i in range(userInput + 1):
        print(fibonacci(i),end=' ')
