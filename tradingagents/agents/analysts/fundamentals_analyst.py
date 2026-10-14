from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_etf_profile,
    get_fundamentals,
    get_income_statement,
    get_instrument_context_from_state,
    get_language_instruction,
)


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
        ]

        system_message = (
            "You are a researcher tasked with analyzing fundamental information over the past week about a company. Please write a comprehensive report of the company's fundamental information such as financial documents, company profile, basic company financials, and company financial history to gain a full view of the company's fundamental information to inform traders. Make sure to include as much detail as possible. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + " Use the available tools: `get_fundamentals` for comprehensive company analysis, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements."
            + get_language_instruction(),
        )

        if state.get("asset_type") == "etf":
            tools = [get_etf_profile]
            system_message = (
                "You are an ETF fundamentals researcher. Call get_etf_profile with the exact ETF symbol "
                "and analysis date before producing your report. Verify fund identity and mandate from "
                "the response. If identity is unverified or mismatched, explicitly withhold a recommendation. "
                "Analyze available benchmark/objective, fees, assets, holdings concentration, allocations, "
                "liquidity and distributions. Treat portfolio P/E as a holdings metric, not fund issuer earnings. "
                "For bond funds consider duration, credit and reinvestment risk; for commodities distinguish "
                "physical holdings from futures roll costs. For leveraged/inverse funds explain daily reset, "
                "path dependence, compounding and derivatives risk; verify the underlying and multiple from "
                "the mandate. Do not apply ordinary long-term buy-and-hold assumptions to daily leveraged funds. "
                "These native metadata snapshots are not point-in-time: do not present them as known on the "
                "analysis date. Label retrieval-time facts separately from dated prices/distributions. "
                "Missing fees, benchmark returns, duration, SEC yield, NAV dates or tracking data stay unknown. "
                "Do not calculate NAV premiums or tracking error from asynchronous or missing observations. "
                "Report only tool-grounded facts, explicitly list material limitations and end with a Markdown table."
                + get_language_instruction()
            )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
