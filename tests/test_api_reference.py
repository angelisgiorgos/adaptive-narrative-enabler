"""docs/ENDPOINTS.md, the per-endpoint reference, is generated from api.py (utils/api_reference.py) and
must be regenerated whenever an endpoint changes: it lists every route once, each with a description."""
try:  # importing the API: recording and downloads go to temporary folders
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import re
import unittest

import api
from utils import api_reference


class EndpointReferenceTests(unittest.TestCase):
    def test_the_reference_is_up_to_date(self):
        current = api_reference.TARGET.read_text(encoding="utf-8")
        self.assertEqual(current, api_reference.render(),
                         "docs/ENDPOINTS.md is out of date: run `python -m utils.api_reference`")

    def test_every_route_is_listed_once_with_a_description(self):
        text = api_reference.TARGET.read_text(encoding="utf-8")
        headings = re.findall(r"^#### `(GET|POST|PUT|PATCH|DELETE) ([^`]+)`$", text, flags=re.M)
        routes = {(method, route.path) for route in api.api.routes if getattr(route, "include_in_schema", False)
                  for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}}
        self.assertEqual(len(headings), len(set(headings)), "an endpoint is listed twice")
        self.assertEqual(sorted(routes - set(headings)), [])
        for block in re.split(r"^#### ", text, flags=re.M)[1:]:
            with self.subTest(endpoint=block.splitlines()[0]):
                self.assertTrue(block.splitlines()[2].strip(), "an endpoint without a description (add a docstring)")


if __name__ == "__main__":
    unittest.main()
