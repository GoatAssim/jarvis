data = open('actions/dev_agent.py', encoding='utf-8').read()
data = data.replace('\\u2013', '\u2013')
open('actions/dev_agent.py', 'w', encoding='utf-8').write(data)
print("remaining backslash-u:", data.count('\\u'))
