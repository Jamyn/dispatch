/**
 * jwt-decode v4 dropped its default export in favour of the named `jwtDecode`.
 * A default import still type-checks and builds, but resolves to undefined and
 * throws only when a user logs in, so this exercises the mutation itself.
 */

import { describe, expect, it, vi } from "vitest"

vi.mock("@/router/index", () => ({ default: { push: vi.fn() } }))
vi.mock("@/auth/api", () => ({ default: { getAll: vi.fn() } }))
vi.mock("@/api", () => ({ default: { get: vi.fn() } }))

globalThis.localStorage = { setItem: vi.fn(), getItem: vi.fn(), removeItem: vi.fn() }

import authStore from "@/auth/store"

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url")
const TOKEN = `${b64({ alg: "HS256", typ: "JWT" })}.${b64({
  email: "user@example.com",
  exp: 4102444800,
})}.signature`

describe("SET_USER_LOGIN", () => {
  it("spreads the decoded token payload onto currentUser", () => {
    const state = { currentUser: { loggedIn: false } }

    authStore.mutations.SET_USER_LOGIN(state, TOKEN)

    expect(state.currentUser.email).toBe("user@example.com")
    expect(state.currentUser.exp).toBe(4102444800)
    expect(state.currentUser.token).toBe(TOKEN)
    expect(state.currentUser.loggedIn).toBe(true)
  })
})
