import assert from "node:assert/strict";
import test from "node:test";
import { IdentityServiceError, resolveVerifiedLogin } from "../src/lib/identity-recovery";

test("verified sign-in reconnects an existing identity when the anonymous workspace no longer exists", async () => {
  const calls: boolean[] = [];
  let discarded = false;
  const principal = await resolveVerifiedLogin(true, async include => {
    calls.push(include);
    if (include) throw new IdentityServiceError(401, "INVALID_IDENTITY_PROOF");
    return { user_id: "original-user" };
  }, async () => { discarded = true; });
  assert.deepEqual(principal, { user_id: "original-user" });
  assert.deepEqual(calls, [true, false]);
  assert.equal(discarded, true);
});

test("valid anonymous continuity is preserved and unrelated authentication failures are not ignored", async () => {
  const discard = async () => { throw new Error("must preserve the cookie"); };
  assert.equal(await resolveVerifiedLogin(true, async include => include, discard), true);
  for (const error of [new IdentityServiceError(403, "SERVICE_IDENTITY_REQUIRED"),
    new IdentityServiceError(401, "OTHER_ERROR"), new Error("Network unavailable")]) {
    let calls = 0;
    await assert.rejects(resolveVerifiedLogin(true, async () => { calls++; throw error; }, discard), e => e === error);
    assert.equal(calls, 1);
  }
});
