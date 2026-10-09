"""The planning step, on the local boss model. IO is fully local: no cloud planner.

Before acting, the boss writes itself a short numbered plan with these instructions; when it gets stuck
(errors in a row, or too many steps), it writes a new one from what it has tried so far.
"""
import re

from openai import OpenAI

SYSTEM = """You plan tasks for a Windows desktop agent. A small local model carries out your plan with these tools:
App (launch or switch to an app by name), Snapshot (text list of open windows and on-screen controls with coordinates),
Click/Type (at coordinates from Snapshot), type_text (type into the focused control), Shortcut (keyboard shortcuts),
Scroll, Wait/WaitFor, PowerShell, Clipboard, Process, find_on_screen (visually locate something not in Snapshot),
look_at_screen (answer a question about what a display shows), browser_open and browser_* tools (open a page in IO's own browser tab, then click and type on it by element),
close_windows (close windows by title, or all_except some), FileSystem (read/write files), Scrape (read a web page as text), ask_user (ask the user a question), remember (save a note),
done (finish, with the answer in its summary).

Write a short numbered plan, at most 8 steps. Each step is one concrete action naming the tool to use.
Prefer App launches, keyboard shortcuts and PowerShell over clicking. Do only what the task asks.
For websites, or when the user mentions the browser, Chrome or IO's tab, start with browser_open and use browser_* tools; never App, Click, Type or Shortcut on a browser window.
PowerShell is a tool that runs a command and returns its output: never open a PowerShell or Terminal window to run one.
What's installed (apps, Steam games), an app's settings or saved data: plan one PowerShell step that reads the registry or the app's
files, not opening the app and clicking through it (e.g. Steam games are the "name" lines in C:\\Program Files (x86)\\Steam\\steamapps\\appmanifest_*.acf).
If the user also asks to open the app, open it too, but still get the facts with PowerShell.
To read text in a window, use Snapshot (it lists the text of controls) or look_at_screen, never select-all and copy: that replaces the user's clipboard.
For games and emulators, use find_on_screen/look_at_screen with window set to the app's title, and close menus with their X button, never Esc/Back.
If a web page turns out to be a list of different meanings (a disambiguation page), plan browser_open on the matching link's address.
Write steps, never answers or facts read from the current state.
The agent itself is called IO. Questions about IO, about the agent, or what it can do are conversation.
If the task names something unfamiliar (a small website, company, product, app, person), plan a web search first:
browser_open https://www.google.com/search?q=<the words> (always Google, never Bing), then browser_open the best result's address if the results aren't enough.
Well-known facts (capitals, famous people and companies, science) need no plan: NO_PLAN.
If the message is conversation rather than something to do on the PC (a greeting, thanks, small talk, or a question
answerable from well-known general knowledge), output exactly NO_PLAN.
When replanning after failures, never repeat a step that failed: use a different tool or source (for a fact, Wikipedia
or a web search; for a site that won't load, another site), or plan done explaining what blocked it.
Output only the plan."""

# With IO's action layer the tool paragraph comes from the same registry as the agent's own tool list (tools_text), so the
# plan only names tools the agent really has; the rules below talk about actions, not the old click-and-type recipes.
SYSTEM_ACTIONS = """You plan tasks for a Windows desktop agent. A small local model carries out your plan.
{tools}

Write a short numbered plan, at most 8 steps. Each step is one concrete action naming the tool to use and its main argument.
Pick the first tool that fits: the higher-level actions find, act and check their own result, so one step often does a whole
job (write_in_app, save_file_as, web_answer, pc_info, app_info). write_in_app opens the app itself: never plan open_app
before it. Do only what the task asks.
Facts about the PC (the time, installed apps, disk space, what's open) come from pc_info, app_info or list_windows, never from
opening an app. If the task says not to open, save or close something, never plan that.
To read text in a window plan read_window, never select-all and copy: that replaces the user's clipboard.
For websites use the web actions (they work in IO's own tab); never drive a Chrome or Edge window.
For games and emulators, use click_on/look_at_screen with window set to the app's title, and close menus with their X button, never Esc/Back.
Write steps, never answers or facts read from the current state.
The agent itself is called IO. Questions about IO, about the agent, or what it can do are conversation.
If the task names something unfamiliar (a small website, company, product, app, person), plan a web search first (always Google, never Bing).
Well-known facts (capitals, famous people and companies, science) need no plan: NO_PLAN.
If the message is conversation rather than something to do on the PC (a greeting, thanks, small talk, or a question
answerable from well-known general knowledge), output exactly NO_PLAN.
When replanning after failures, never repeat a step that failed: follow the failed result's try: hint or use a different
tool, or plan done explaining what blocked it.
Output only the plan."""


def plan_local(task: str, context: str, base_url: str, model: str, history: str = "", tools_text: str = "",
               reasoning: dict | None = None) -> str:
    """A plan from the local boss model. "" when it says NO_PLAN. history: recent actions, when replanning.
    tools_text: the agent's tool paragraph from the action registry (replaces the hand-written one)."""
    system = SYSTEM_ACTIONS.format(tools=tools_text) if tools_text else SYSTEM
    user = f"Task: {task}"
    if context:
        user += f"\n\nCurrent state of the PC:\n{context}"
    if history:
        # the results themselves, not just the call names: a replan that couldn't see what had been found planned the
        # finding all over again (step 1 was always the first web search)
        user += (f"\n\nWhat the agent has done so far, with the results:\n{history}\n\nPlan only what "
                 "is left: anything these results already show (facts found, folders made, files written) is done, so never "
                 "plan it again, and use the facts found as they are.")
    client = OpenAI(base_url=base_url, api_key="local", max_retries=1, timeout=90)
    # reasoning: the llama-server fields for the model's thinking (boss passes Glimmer's, low); its thinking counts
    # against max_tokens, which is why there is room for it above the plan's own few hundred tokens
    reply = client.chat.completions.create(model=model, temperature=0.2, max_tokens=1600 if reasoning else 400, extra_body=reasoning or None,
                                           messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    text = re.sub(r"<think>.*?</think>", "", reply.choices[0].message.content or "", flags=re.S).strip()
    return "" if text.upper().startswith("NO_PLAN") else text
