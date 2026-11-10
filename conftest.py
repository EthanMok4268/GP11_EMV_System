import re

import pytest
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers, MultiDict


class FormClient(FlaskClient):
    """Supply the browser's CSRF token for existing form tests."""

    def post(self, path, *, data=None, headers=None, **kwargs):
        page = self.get("/login", base_url=kwargs.get("base_url"))
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        assert token is not None, "Login page is missing its CSRF token"

        form = MultiDict(data)
        form.setdefault("csrf_token", token[1].decode())
        headers = Headers(headers)
        headers.setdefault("Referer", page.request.url)
        return super().post(path, data=form, headers=headers, **kwargs)


@pytest.fixture
def form_client_class():
    return FormClient
