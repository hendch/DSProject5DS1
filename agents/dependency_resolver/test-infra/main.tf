resource "terraform_data" "cluster_role" {
  input = terraform_data.cluster_policy.id
}

resource "terraform_data" "cluster" {
  input = terraform_data.cluster_role.id
}

resource "terraform_data" "cluster_policy" {
  input = terraform_data.cluster.id
}