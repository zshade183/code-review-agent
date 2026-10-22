import requests

# 测试代码审查功能
url = 'http://127.0.0.1:8000/review'
data = {
    'code': 'def test(a,b,c):\n    if a>10:\n        print("A太大小")\n    sum = a+b+c\n    print(sum)'
}

response = requests.post(url, json=data)
print(f"Status Code: {response.status_code}")
print(f"Response: {response.json()}")