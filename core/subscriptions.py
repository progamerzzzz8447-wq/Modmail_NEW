def author_has_thread_subscription(subscriptions, author) -> bool:
    """Return whether an author is covered by a user, role, or guild-wide subscription."""
    subscribed_mentions = {str(value).strip() for value in subscriptions or ()}
    if not subscribed_mentions or author is None:
        return False
    if subscribed_mentions.intersection({"@here", "@everyone"}):
        return True

    author_id = getattr(author, "id", None)
    if author_id is not None and subscribed_mentions.intersection(
        {f"<@{author_id}>", f"<@!{author_id}>"}
    ):
        return True

    return any(
        getattr(role, "mention", f"<@&{getattr(role, 'id', '')}>")
        in subscribed_mentions
        for role in (getattr(author, "roles", None) or ())
    )
