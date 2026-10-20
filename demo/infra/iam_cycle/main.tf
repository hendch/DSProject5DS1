# Demo for the orchestrator's "failed deploy" scenario: the checkout cluster needs its
# IAM role, the role's policy points at the cluster, and the policy is attached back to
# the role, so nothing can be created first. The Dependency Resolver finds the cycle;
# because it touches IAM, the Guardrail raises the risk and holds the fix for a human.

resource "aws_iam_role" "checkout" {
  name               = "checkout-cluster"
  assume_role_policy = aws_iam_role_policy.checkout.policy
}

resource "aws_eks_cluster" "checkout" {
  name     = "checkout"
  role_arn = aws_iam_role.checkout.arn
}

resource "aws_iam_role_policy" "checkout" {
  name   = "checkout-access"
  role   = aws_iam_role.checkout.id
  policy = jsonencode({ Resource = aws_eks_cluster.checkout.arn })
}
