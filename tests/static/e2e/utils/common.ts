import { randomUUID } from "node:crypto"

// Fixed 32 chars. The previous Math.random/base36 form returned 2-6 characters
// depending on the draw, which made the account it names unpredictable in
// length rather than just in value.
export function generateRandomString(): string {
  return randomUUID().replace(/-/g, "")
}
