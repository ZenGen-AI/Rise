import { describe, it, expect } from "vitest";
import { CLI_NAME } from "../src/index.js";

describe("cli smoke", () => {
  it("exposes the cli name", () => {
    expect(CLI_NAME).toBe("rismforge");
  });
});
