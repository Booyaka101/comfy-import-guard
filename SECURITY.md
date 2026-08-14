# Security Policy

## Supported versions

The latest published version is the only one that gets fixes.

## Reporting a vulnerability

Please **don't** open a public issue for a security problem.

Use GitHub's [private vulnerability reporting](https://github.com/Booyaka101/comfy-import-guard/security/advisories/new) instead. Expect a first response within a week.

Please include what you found, how to reproduce it, and what an attacker gets out of it.

## What this touches

Reads your ComfyUI install and the custom-node repos in it. It does not start ComfyUI and does not run node code.

- **It does not import the node packs it analyses.** Analysis is static; a pack cannot execute code by being scanned.

## Scope

In scope: anything that leaks a credential, reads data belonging to someone else, or lets untrusted input reach code execution.

Out of scope: findings that require an attacker to already control the machine it runs on.
