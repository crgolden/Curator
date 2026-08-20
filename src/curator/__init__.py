"""Curator: a multi-user PlayStation library curation API.

Sits behind Duende IdentityServer (OIDC) as its auth front door, links each authenticated user's PSN
account, and persists a shared game catalog plus per-user library state in PostgreSQL.
"""
