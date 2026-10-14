"""Layer 2 (validation + risk scoring). Pure functions: no services needed."""
import unittest

from agents.guardrail.risk import assess, validate
from agents.guardrail.tests.support import event


class Validate(unittest.TestCase):
    def test_good_event(self):
        self.assertEqual(validate(event()), [])

    def test_missing_and_invalid_fields(self):
        e = event(environment="moon", blast_radius="tiny")
        del e["correlation_id"]
        problems = validate(e)
        self.assertIn("missing field 'correlation_id'", problems)
        self.assertIn("invalid environment 'moon'", problems)
        self.assertIn("invalid blast_radius 'tiny'", problems)

    def test_not_an_object(self):
        self.assertEqual(validate(["x"]), ["event is not a JSON object"])
        self.assertIn("payload is not an object", validate(event() | {"payload": "text"}))


class Assess(unittest.TestCase):
    def test_plain_dev_fix_stays_low(self):
        self.assertEqual(assess(event())[0], "low")

    def test_iam_raises_to_high(self):
        risk, signals = assess(event(payload={"cycle": ["aws_iam_role.checkout", "aws_eks_cluster.x"]}))
        self.assertEqual(risk, "high")
        self.assertIn("changes permissions or security boundaries", [s["signal"] for s in signals])

    def test_prod_and_destructive_raise_to_high(self):
        self.assertEqual(assess(event(payload={"proposed_diff": "edit prod.tfvars"}))[0], "high")
        self.assertEqual(assess(event(payload={"proposed_diff": "terraform destroy the db"}))[0], "high")

    def test_word_containing_prod_is_not_prod(self):
        self.assertEqual(assess(event(payload={"proposed_diff": "update the product page"}))[0], "low")

    def test_billable_is_medium(self):
        self.assertEqual(assess(event(payload={"cycle": ["aws_nat_gateway.main"]}))[0], "medium")
        self.assertEqual(assess(event(payload={"proposed_diff": "instance_type = p3.2xlarge"}))[0], "medium")

    def test_names_inside_resource_addresses_are_found(self):
        for text in ("aws_kms_key.main", "google_project_iam_member.x", "aws_db.app_prod_db",
                     "azurerm_role_assignment.x", "aws_secretsmanager_secret.db"):
            with self.subTest(text):
                self.assertEqual(assess(event(payload={"cycle": [text]}))[0], "high")

    def test_no_false_alarm_on_similar_words(self):
        for text in ("product_catalog", "iambic", "terraform_data.cluster_role", "deleted_flag_column"):
            with self.subTest(text):
                self.assertEqual(assess(event(payload={"cycle": [text]}))[0], "low")

    def test_fallback_output_bumps_one_level(self):
        e = event(payload={"sources": {"detection": "static", "classification": "heuristic"}})
        risk, signals = assess(e)
        self.assertEqual(risk, "medium")
        self.assertEqual(signals[-1]["evidence"], "classification, detection")

    def test_never_lowers_the_agents_label(self):
        self.assertEqual(assess(event(blast_radius="high"))[0], "high")

    def test_advice_is_not_scanned(self):
        e = event(agent_name="diagnostic_rca", action_type="rank_causes",
                  payload={"failure_summary": "prod db delete failed", "ranked_causes": []})
        self.assertEqual(assess(e)[0], "low")


if __name__ == "__main__":
    unittest.main()
