#n=123
n=int(input("Enter Number"))
rev=0
while n!=0:
    d=n%10
    rev=rev*10+d
    n=n//10

print(rev)
