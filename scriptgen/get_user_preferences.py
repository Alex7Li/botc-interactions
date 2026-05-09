import json
import requests
from pathlib import Path

def main():
    URL = "https://clockfaceoff.com/api/rankings/global"
    response = requests.get(URL, verify=False)
    response_obj = response.json()
    rankings = {}
    for char_data in response_obj['rankings']:
        rankings[char_data['name']] = char_data['rating']

    with open(Path(__file__).parent / "char_rankings.json", "w") as f:
        json.dump(rankings, f, indent=2)

if __name__ == "__main__":
    main()
