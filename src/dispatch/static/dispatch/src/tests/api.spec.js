import { expect, test, vi, beforeEach } from "vitest"

const commitMock = vi.fn()
const routerPushMock = vi.fn()
const routerGoMock = vi.fn()

vi.mock("@/store", () => ({
  default: { commit: (...args) => commitMock(...args) },
}))

vi.mock("@/auth/store", () => ({
  default: { state: { currentUser: { token: "test-token" } } },
}))

vi.mock("@/router", () => ({
  default: {
    push: (...args) => routerPushMock(...args),
    go: (...args) => routerGoMock(...args),
    currentRoute: { value: { params: { organization: "default" } } },
  },
}))

import instance from "@/api"

beforeEach(() => {
  vi.clearAllMocks()
})

// api.js registers its error-handling interceptor via
// instance.interceptors.response.use(onFulfilled, onRejected) -- this pulls
// that onRejected handler straight off the live axios instance so the test
// runs the actual code path a real HTTP error triggers, not a copy of it.
function getResponseRejectedHandler() {
  return instance.interceptors.response.handlers[0].rejected
}

function axiosError(status, data, config = {}) {
  return { response: { status, data, headers: {} }, config }
}

test("403 response pushes a notification with the joined detail messages", async () => {
  const rejected = getResponseRejectedHandler()
  await expect(
    rejected(axiosError(403, { detail: [{ msg: "forbidden" }, { msg: "try again" }] }))
  ).rejects.toBeTruthy()

  expect(commitMock).toHaveBeenCalledWith(
    "notification_backend/addBeNotification",
    { text: "forbidden try again", type: "exception" },
    { root: true }
  )
})

test("422 response pushes a notification with the joined detail messages", async () => {
  const rejected = getResponseRejectedHandler()
  await expect(
    rejected(axiosError(422, { detail: [{ msg: "invalid field" }] }))
  ).rejects.toBeTruthy()

  expect(commitMock).toHaveBeenCalledWith(
    "notification_backend/addBeNotification",
    { text: "invalid field", type: "exception" },
    { root: true }
  )
})

test("500 response with no detail falls back to a generic message", async () => {
  const rejected = getResponseRejectedHandler()
  await expect(rejected(axiosError(500, {}))).rejects.toBeTruthy()

  expect(commitMock).toHaveBeenCalledWith(
    "notification_backend/addBeNotification",
    {
      text: "Something has gone wrong. Please, retry or let your admin know that you received this error.",
      type: "exception",
    },
    { root: true }
  )
})

test("401 response on the basic auth provider redirects to login", async () => {
  const rejected = getResponseRejectedHandler()
  await expect(rejected(axiosError(401, { detail: [] }))).rejects.toBeTruthy()

  expect(routerPushMock).toHaveBeenCalledWith({ name: "BasicLogin" })
})

test("errorHandle: false on the request config bypasses notification handling", async () => {
  const rejected = getResponseRejectedHandler()
  await expect(
    rejected(axiosError(403, { detail: [{ msg: "should not notify" }] }, { errorHandle: false }))
  ).rejects.toBeTruthy()

  expect(commitMock).not.toHaveBeenCalled()
})
