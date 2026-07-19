import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { waitForJob } from "./AuditPanel";

describe("waitForJob", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("keeps monitoring beyond two minutes until the server is terminal", async () => {
    vi.useFakeTimers();
    let calls = 0;
    vi.spyOn(api, "job").mockImplementation(async (jobId) => {
      calls += 1;
      return {
        job_id: jobId,
        status: calls > 121 ? "succeeded" : "running",
        raw: {},
      };
    });

    const result = waitForJob({ job_id: "long-audit", status: "running", raw: {} });
    for (let second = 0; second < 122; second += 1) {
      await vi.advanceTimersByTimeAsync(1_000);
    }

    await expect(result).resolves.toMatchObject({ status: "succeeded" });
    expect(calls).toBe(122);
  });
});
