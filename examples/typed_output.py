"""Get the agent's answer as typed data, not only text.

Pass a Pydantic model as `output_format`. The answer arrives in
`ResultMessage.structured_output` as an instance of that model.

    uv run --env-file .env examples/typed_output.py
"""

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from bob_agent_sdk import BobAgentOptions, ModeDefinition, ResultMessage, query


class Finding(BaseModel):
    file: str
    line: int = Field(ge=1)
    severity: Literal["low", "medium", "high"]
    summary: str = Field(description="One sentence")


class Review(BaseModel):
    findings: list[Finding] = Field(max_length=3)
    verdict: Literal["approve", "request_changes"]


async def main() -> None:
    options = BobAgentOptions(
        cwd="src/bob_agent_sdk",
        mode=ModeDefinition(
            role_definition="You are a code reviewer. You read code and report risks.",
            groups=["read"],  # Read-only. Typed output works without more tool groups.
        ),
        output_format=Review,
    )
    async for message in query(prompt="Review _http.py for security problems.", options=options):
        if isinstance(message, ResultMessage):
            review: Review = message.structured_output
            print(f"Verdict: {review.verdict}")
            for finding in review.findings:
                print(f"  [{finding.severity}] {finding.file}:{finding.line}  {finding.summary}")


if __name__ == "__main__":
    asyncio.run(main())
