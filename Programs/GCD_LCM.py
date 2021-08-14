a=int(input("ENter first number"))
b=int(input("ENter Second number"))
n1=a
n2=b
while n1!=n2:
    if n1>n2:
        n1=n1-n2
    else:
        n2=n2-n1
gcd=n1
lcm=(a*b)/gcd
print("GCD=",gcd)
print("LCM=",lcm)
