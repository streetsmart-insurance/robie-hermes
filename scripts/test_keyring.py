import keyring

print("Current backend:", keyring.get_keyring())
for svc in ["Coterie", "The Hartford", "TAPCO", "StreetSmartInsurance", "EZLynx", "coterieinsurance.com", "thehartford.com", "gotapco.com"]:
    user = keyring.get_password(svc, "username")
    pwd = keyring.get_password(svc, "password")
    print(f"Service: {svc} -> user={user}, pwd={'***' if pwd else None}")
