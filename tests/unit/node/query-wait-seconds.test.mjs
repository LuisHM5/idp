import test from "node:test";
import assert from "node:assert/strict";

import { parseWaitSeconds } from "../../../src/document-processing/resources/query-lambda/wait-seconds.mjs";

test("waitSeconds defaults to zero and accepts the documented range", () => {
  assert.deepEqual(parseWaitSeconds(undefined), { seconds: 0 });
  assert.deepEqual(parseWaitSeconds("0"), { seconds: 0 });
  assert.deepEqual(parseWaitSeconds("10"), { seconds: 10 });
  assert.deepEqual(parseWaitSeconds("20"), { seconds: 20 });
  assert.deepEqual(parseWaitSeconds("0.25"), { seconds: 0.25 });
});

test("waitSeconds rejects malformed and out-of-range values", () => {
  for (const value of ["-1", "21", "abc", "1s", "NaN"]) {
    assert.equal(typeof parseWaitSeconds(value).error, "string", value);
  }
});
