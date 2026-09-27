
def process_item(item):
    item["_n"] = item.get("_n", 0) + 1
    return item
