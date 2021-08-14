def fibonacci_without_recursion(number):
    t1=0
    t2=1
    i=0
    while i<number:
        print(t1)
        t3=t1+t2
        t1=t2
        t2=t3
        i=i+1
    
    	

userInput = int(input('Enter the number upto which you wish to calculate fibonnaci series: '))
print("\nUsing LOOP:")
print(fibonacci_without_recursion(userInput))
