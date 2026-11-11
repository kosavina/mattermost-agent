"""The processor: takes ready jobs from the listener's queue and runs an agent on each one.

It holds no Mattermost token and makes no Mattermost call. Nothing in this package imports the
Mattermost client, and a test checks that.
"""
