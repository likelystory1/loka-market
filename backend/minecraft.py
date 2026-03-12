import requests

cache={}

def uuid_to_name(uuid):

    if not uuid:
        return "Unknown"

    if uuid in cache:
        return cache[uuid]

    try:

        url=f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid.replace('-','')}"

        r=requests.get(url)

        name=r.json()["name"]

        cache[uuid]=name

        return name

    except:

        return "Unknown"
