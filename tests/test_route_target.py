"""The one untrusted input on the HTTP route is the ``target`` query param.

It is passed to git as a positional argv token, never through a shell, but the
route still rejects anything that is not ref-shaped before git sees it, so an
option-injection string can never reach the git command line.
"""

from comfy_import_guard import _REF


def valid(t):
    return bool(_REF.match(t))


def test_real_refs_pass():
    for t in ["origin/master", "master", "HEAD", "HEAD~3", "v0.32.0",
              "555b82b2af2de74c8b3aa0ae9669e799c9054e34",
              "origin/master^{commit}", "release/1.0", "user@sha"]:
        assert valid(t), t


def test_option_shaped_input_rejected():
    for t in ["--open-files-in-pager=touch x", "--output=/tmp/x", "-O touch x",
              "--upload-pack=touch x", "-c core.pager=touch x"]:
        assert not valid(t), t


def test_shell_metacharacters_rejected():
    for t in ["; touch x", "$(touch x)", "`touch x`", "a && b", "a|b",
              "ext::sh -c touch", "a b", "a\nb", ""]:
        assert not valid(t), t
