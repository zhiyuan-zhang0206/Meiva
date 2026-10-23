import os

from base.db import NoDatabaseAuthorityError
from meiva import impersonation

before = dict(os.environ)
try:
    impersonation.attach("lease")
except NoDatabaseAuthorityError as error:
    assert "startup entry" in str(error)
else:
    raise AssertionError("undelivered login must be refused")
assert dict(os.environ) == before
