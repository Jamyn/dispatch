"""Build the createCommitOnBranch GraphQL body for the Dependabot lockfile fix.

Kept out of the workflow's `run:` block so the branch name reaches the mutation
as data. Interpolating `head.ref` into a shell script would make a branch name
executable, and Dependabot branch names are derived from package names.
"""

import base64
import json
import os

MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}
"""


def main() -> None:
    path = os.environ["LOCKFILE"]
    with open(path, "rb") as handle:
        contents = base64.b64encode(handle.read()).decode()

    body = {
        "query": MUTATION,
        "variables": {
            "input": {
                "branch": {
                    "repositoryNameWithOwner": os.environ["GITHUB_REPOSITORY"],
                    "branchName": os.environ["BRANCH"],
                },
                # Fails the mutation if Dependabot force-pushed while this ran,
                # rather than clobbering whatever landed in the meantime.
                "expectedHeadOid": os.environ["HEAD_SHA"],
                "message": {"headline": "build(deps): restore lockfile dev flags"},
                "fileChanges": {"additions": [{"path": path, "contents": contents}]},
            }
        },
    }
    print(json.dumps(body))


if __name__ == "__main__":
    main()
