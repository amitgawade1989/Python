
def charFrequency(userInput):
    '''This fuction helps to count the char frequency in the given string '''
    userInput = userInput.lower() #covert to lowercase
    dict = {}
    for char in userInput:
       # keys = dict.keys()
        if char in keys:
            dict[char] += 1
        else:
            dict[char] = 1
    return dict
userInput = str(input('Enter a string: '))
print(charFrequency(userInput))
