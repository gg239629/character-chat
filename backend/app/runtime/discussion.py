"""LangGraph speaker nomination. Character nodes still stream and commit individually."""
from typing import TypedDict
from langgraph.graph import StateGraph,START,END
from backend.app.characters.actors import ROLES


class Discussion(TypedDict,total=False):
    pending:list[str]
    required:list[str]
    declined:list[str]
    last:str|None
    emitted:int
    attempts:int
    role:str|None
    forced:bool


def build(planner,max_messages,max_attempts):
    async def budget(state):return {'role':None,'attempts':state['attempts']+1}
    async def nominate(state):
        required=list(state['required'])
        if required:return {'role':required.pop(0),'required':required,'forced':True}
        pending=list(state['pending'])
        if not pending:pending=list(await planner(state))
        return {'pending':pending,'forced':False}
    async def validate(state):
        if state.get('forced'):return {'role':state['role'] if state['role'] in ROLES else None}
        pending=[role for role in state['pending'] if role in ROLES and role not in state['declined'] and role!=state['last']]
        return {'role':pending.pop(0) if pending else None,'pending':pending}
    graph=StateGraph(Discussion)
    graph.add_node('budget',budget);graph.add_node('nominate',nominate);graph.add_node('validate',validate)
    graph.add_edge(START,'budget')
    graph.add_conditional_edges('budget',lambda s:'stop' if s['emitted']>=max_messages or s['attempts']>max_attempts else 'next',{'stop':END,'next':'nominate'})
    graph.add_edge('nominate','validate');graph.add_edge('validate',END)
    return graph.compile()
