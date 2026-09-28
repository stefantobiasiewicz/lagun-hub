"""The hub core: the installation model, threads, transactions, configuration,
persistence.

What is not here and is not to be: **any knowledge of who talks to the hub**. The
adapters — CLI, HTTP, MQTT — sit next door and reach in only through `lagunhub.api`.
"""
