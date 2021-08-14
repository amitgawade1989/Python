#n=123
n=int(input("Enter Number"))
rev=0
temp=n
while n!=0:
    d=n%10
    rev=rev*10+d
    n=n//10

if temp==rev:
    print("no is palindrom")
else:
    print("no is not palindrom")
