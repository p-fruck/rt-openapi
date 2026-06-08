import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parent / "extract_routes.py"
SPEC = importlib.util.spec_from_file_location("extract_routes", MODULE_PATH)
extract_routes = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(extract_routes)


class ExtractRoutesRegressionTests(unittest.TestCase):
    def _ticket_history_entry(self):
        return {
            "path": "/ticket/{id2}/history",
            "resources": ["RT::REST2::Resource::Transactions"],
            "roles": [
                "RT::REST2::Resource::Collection::QueryByJSON",
                "RT::REST2::Resource::Collection::QueryBySQL",
            ],
        }

    def test_ticket_history_description_warns_content_may_be_empty(self):
        entry = self._ticket_history_entry()
        description = extract_routes.build_operation_description(entry, entry["path"], "GET")

        self.assertIn("Ticket comments are transactions where `Type` is `Comment`", description)
        self.assertIn("Requested fields can be present but empty", description)
        self.assertIn("first filter to `Type=Comment`", description)
        self.assertIn("retrieve the comment body from attachments", description)

    def test_ticket_history_default_filter_targets_comment(self):
        entry = self._ticket_history_entry()
        example = extract_routes.default_query_filter_example(entry, entry["path"])

        self.assertEqual(example, {"field": "Type", "operator": "=", "value": "Comment"})

    def test_transaction_collection_schema_selected_for_ticket_history(self):
        entry = self._ticket_history_entry()

        schema_ref_get = extract_routes.response_schema_ref_for_entry(entry, "GET")
        schema_ref_post = extract_routes.response_schema_ref_for_entry(entry, "POST")

        self.assertEqual(schema_ref_get, "#/components/schemas/TransactionCollectionResponse")
        self.assertEqual(schema_ref_post, "#/components/schemas/TransactionCollectionResponse")

    def test_extract_core_accessible_fields_parses_types(self):
        perl_text = """
package RT::Sample;
sub _CoreAccessible {
    {
        id => { read => 1, type => 'int(11)', default => '' },
        Subject => { read => 1, type => 'varchar(200)', default => '' },
        Created => { read => 1, type => 'datetime', default => '' },
        OptionalField => { read => 1, type => 'varchar(64)', default => undef },
        WriteOnly => { write => 1, type => 'varchar(64)', default => '' },
    }
}
"""
        fields = extract_routes.extract_core_accessible_fields(perl_text)

        self.assertIn("id", fields)
        self.assertIn("Subject", fields)
        self.assertIn("Created", fields)
        self.assertIn("OptionalField", fields)
        self.assertNotIn("WriteOnly", fields)

        self.assertEqual(fields["id"]["schema"]["type"], "integer")
        self.assertEqual(fields["Subject"]["schema"]["type"], "string")
        self.assertEqual(fields["Created"]["schema"]["format"], "date-time")
        self.assertEqual(fields["OptionalField"]["schema"]["type"], ["string", "null"])

    def test_perl_type_to_openapi_schema_maps_date(self):
        schema = extract_routes.perl_type_to_openapi_schema("date", False, False)
        self.assertEqual(schema, {"type": "string", "format": "date"})

    def test_ticket_comment_request_schema_requires_content_type(self):
        entry = {
            "path": "/ticket/{id1}/comment",
            "resources": ["RT::REST2::Resource::Ticket"],
            "roles": ["RT::REST2::Resource::Record::Update"],
        }
        schema = extract_routes.request_body_schema_for_operation(
            entry,
            "/ticket/{id1}/comment",
            "POST",
            None,
        )
        self.assertEqual(schema, {"$ref": "#/components/schemas/TicketCommentCreateRequest"})

    def test_ticket_create_request_schema_is_specialized(self):
        entry = {
            "path": "/ticket",
            "resources": ["RT::REST2::Resource::Tickets"],
            "roles": ["RT::REST2::Resource::Collection::QueryByJSON"],
        }
        schema = extract_routes.request_body_schema_for_operation(
            entry,
            "/ticket",
            "POST",
            None,
        )
        self.assertEqual(schema, {"$ref": "#/components/schemas/TicketCreateRequest"})


if __name__ == "__main__":
    unittest.main()
