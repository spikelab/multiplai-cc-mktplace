def display_name(store, user_id):
    try:
        return store.user(str(user_id))["name"]
    except KeyError:
        return "unknown user"
