"""A small CP-SAT production scheduler exposed over MCP stdio."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any

from ortools.sat.python import cp_model
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("production-scheduling-mcp")
DEFAULT_START = "2026-01-05T08:00"
MINUTES_PER_HOUR = 60


def _invalid(message: str) -> dict[str, Any]:
    return {"status": "INVALID", "message": message}


def _minutes(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number of hours")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a number of hours") from None
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        qualifier = "greater than zero" if positive else "zero or greater"
        raise ValueError(f"{field} must be {qualifier}")
    # Round to the nearest whole minute; durations and times share this rule.
    minutes = int((number * MINUTES_PER_HOUR).quantize(Decimal("1")))
    if positive and minutes < 1:
        raise ValueError(f"{field} must be at least one minute")
    return minutes


def _label(start: datetime, minute: int) -> str:
    return (start + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M")


def _parse(problem: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], datetime]:
    if not isinstance(problem, dict):
        raise ValueError("problem must be an object with machines and orders")
    machines, orders = problem.get("machines"), problem.get("orders")
    if not isinstance(machines, list) or not machines:
        raise ValueError("machines must be a non-empty list")
    if not isinstance(orders, list) or not orders:
        raise ValueError("orders must be a non-empty list")
    try:
        start = datetime.fromisoformat(problem.get("start", DEFAULT_START))
    except (TypeError, ValueError):
        raise ValueError("start must be an ISO datetime") from None

    parsed_machines: list[dict[str, Any]] = []
    machine_names: set[str] = set()
    for index, machine in enumerate(machines):
        if not isinstance(machine, dict) or not isinstance(machine.get("name"), str) or not machine["name"].strip():
            raise ValueError(f"machines[{index}].name is required")
        name = machine["name"].strip()
        if name in machine_names:
            raise ValueError(f"machine name '{name}' is duplicated")
        machine_names.add(name)
        downs = machine.get("down_times", [])
        if not isinstance(downs, list):
            raise ValueError(f"machines[{index}].down_times must be a list")
        parsed_downs = []
        for j, pair in enumerate(downs):
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(f"machines[{index}].down_times[{j}] must be [from_hour, to_hour]")
            left = _minutes(pair[0], f"machines[{index}].down_times[{j}][0]")
            right = _minutes(pair[1], f"machines[{index}].down_times[{j}][1]")
            if right <= left:
                raise ValueError(f"machines[{index}].down_times[{j}] end must be after its start")
            parsed_downs.append((left, right))
        parsed_machines.append({"name": name, "down_times": parsed_downs})

    parsed_orders = []
    order_names: set[str] = set()
    for index, order in enumerate(orders):
        if not isinstance(order, dict) or not isinstance(order.get("name"), str) or not order["name"].strip():
            raise ValueError(f"orders[{index}].name is required")
        name = order["name"].strip()
        if name in order_names:
            raise ValueError(f"order name '{name}' is duplicated")
        order_names.add(name)
        due = order.get("due_hour")
        due_minute = None if due is None else _minutes(due, f"orders[{index}].due_hour")
        priority = order.get("priority", 1)
        if isinstance(priority, bool):
            raise ValueError(f"orders[{index}].priority must be a positive number")
        try:
            priority_num = float(priority)
        except (ValueError, TypeError):
            raise ValueError(f"orders[{index}].priority must be a positive number") from None
        if not math.isfinite(priority_num) or priority_num <= 0:
            raise ValueError(f"orders[{index}].priority must be a positive number")
        # CP-SAT uses integer objective coefficients. Preserve relative weights to 3 decimals.
        weight = max(1, round(priority_num * 1000))
        steps = order.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"orders[{index}].steps must be a non-empty list")
        parsed_steps = []
        for step_index, step in enumerate(steps):
            prefix = f"orders[{index}].steps[{step_index}]"
            if not isinstance(step, dict):
                raise ValueError(f"{prefix} must be an object")
            if "hours" not in step:
                raise ValueError(f"{prefix}.hours is required")
            duration = _minutes(step["hours"], f"{prefix}.hours", positive=True)
            eligible = step.get("machines")
            if not isinstance(eligible, list) or not eligible:
                raise ValueError(f"{prefix}.machines must be a non-empty list")
            if any(not isinstance(x, str) or x not in machine_names for x in eligible):
                raise ValueError(f"{prefix}.machines must name machines in the machines list")
            parsed_steps.append({"hours": duration, "machines": list(dict.fromkeys(eligible))})
        parsed_orders.append({"name": name, "due": due_minute, "priority": weight, "steps": parsed_steps})
    return parsed_machines, parsed_orders, start


def _solve_schedule_core(problem: dict[str, Any]) -> dict[str, Any]:
    """Find a schedule that minimizes weighted lateness, then final finish time."""
    try:
        machines, orders, start = _parse(problem)
    except (ValueError, OverflowError) as exc:
        return _invalid(str(exc))

    horizon = sum(step["hours"] for order in orders for step in order["steps"])
    horizon += sum(end - begin for machine in machines for begin, end in machine["down_times"])
    horizon = max(horizon * 2 + 1, 1)
    model = cp_model.CpModel()
    machine_intervals: dict[str, list[Any]] = {machine["name"]: [] for machine in machines}
    ops: dict[tuple[int, int], dict[str, Any]] = {}
    late_vars: list[Any] = []
    late_weights: list[int] = []
    end_vars = []

    for machine in machines:
        for index, (begin, end) in enumerate(machine["down_times"]):
            interval = model.new_fixed_size_interval_var(begin, end - begin, f"down_{machine['name']}_{index}")
            machine_intervals[machine["name"]].append(interval)

    for oi, order in enumerate(orders):
        previous_end = None
        for si, step in enumerate(order["steps"]):
            begin = model.new_int_var(0, horizon, f"start_{oi}_{si}")
            end = model.new_int_var(0, horizon, f"end_{oi}_{si}")
            model.new_interval_var(begin, step["hours"], end, f"op_{oi}_{si}")
            if previous_end is not None:
                model.add(begin >= previous_end)
            previous_end = end
            choices = []
            for mi, machine_name in enumerate(step["machines"]):
                present = model.new_bool_var(f"on_{oi}_{si}_{mi}")
                interval = model.new_optional_interval_var(begin, step["hours"], end, present, f"opt_{oi}_{si}_{mi}")
                machine_intervals[machine_name].append(interval)
                choices.append(present)
            model.add_exactly_one(choices)
            ops[(oi, si)] = {"start": begin, "end": end, "choices": choices,
                             "machines": step["machines"], "duration": step["hours"]}
        finish = previous_end
        end_vars.append(finish)
        if order["due"] is not None:
            late = model.new_int_var(0, horizon, f"late_{oi}")
            model.add_max_equality(late, [finish - order["due"], 0])
            late_vars.append(late)
            late_weights.append(order["priority"])
    for intervals in machine_intervals.values():
        model.add_no_overlap(intervals)
    makespan = model.new_int_var(0, horizon, "finish_time")
    model.add_max_equality(makespan, end_vars)
    late_objective = sum(var * weight for var, weight in zip(late_vars, late_weights))
    model.minimize(late_objective)

    def run(solver: cp_model.CpSolver) -> cp_model.CpSolverStatus:
        solver.parameters.max_time_in_seconds = 5.0
        solver.parameters.num_search_workers = 8
        solver.parameters.log_search_progress = False
        return solver.solve(model)

    solver = cp_model.CpSolver()
    status = run(solver)
    if status == cp_model.INFEASIBLE:
        return {"status": "NO_SOLUTION", "message": "No feasible schedule was found."}
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "NO_SOLUTION", "message": "No schedule was found within the time limit."}
    primary_optimal = status == cp_model.OPTIMAL
    best_lateness = int(round(solver.objective_value))
    if primary_optimal:
        model.add(late_objective == best_lateness)
        model.minimize(makespan)
        second = cp_model.CpSolver()
        second_status = run(second)
        if second_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            solver = second
        primary_optimal = primary_optimal and second_status == cp_model.OPTIMAL
    final_status = "OPTIMAL" if primary_optimal else "FEASIBLE"

    plan: dict[str, list[dict[str, Any]]] = {machine["name"]: [] for machine in machines}
    task_records: list[dict[str, Any]] = []
    per_order: dict[int, list[dict[str, Any]]] = {i: [] for i in range(len(orders))}
    for (oi, si), op in ops.items():
        selected = next(i for i, choice in enumerate(op["choices"]) if solver.boolean_value(choice))
        machine_name = op["machines"][selected]
        begin, end = solver.value(op["start"]), solver.value(op["end"])
        record = {"order": orders[oi]["name"], "step": si + 1,
                  "start_minute": begin, "end_minute": end,
                  "start": _label(start, begin), "end": _label(start, end),
                  "hours": op["duration"] / MINUTES_PER_HOUR}
        plan[machine_name].append(record)
        per_order[oi].append({**record, "machine": machine_name})
        task_records.append({**record, "machine": machine_name})
    for records in plan.values():
        records.sort(key=lambda item: (item["start_minute"], item["end_minute"]))
    for records in per_order.values():
        records.sort(key=lambda item: item["step"])
    finish_minute = solver.value(makespan)
    late_orders = []
    for oi, order in enumerate(orders):
        if order["due"] is None:
            continue
        finish = per_order[oi][-1]["end_minute"]
        if finish > order["due"]:
            late_step = next((r for r in per_order[oi] if r["end_minute"] > order["due"]), per_order[oi][-1])
            machine_name = late_step["machine"]
            before = [r for r in plan[machine_name] if r["end_minute"] <= late_step["start_minute"] and r["order"] != order["name"]]
            downtime = [(a, b) for m in machines if m["name"] == machine_name for a, b in m["down_times"]
                        if a < late_step["start_minute"] and b <= late_step["start_minute"]]
            if downtime and (not before or max(b for _, b in downtime) > max(r["end_minute"] for r in before)):
                reason = f"{machine_name} was down until {_label(start, max(b for _, b in downtime))} before this step."
            elif before:
                previous = max(before, key=lambda r: r["end_minute"])
                reason = f"{machine_name} was busy with {previous['order']} until {previous['end']} before this step."
            else:
                reason = f"The required steps could not finish before the due time."
            late_orders.append({"name": order["name"], "due": _label(start, order["due"]),
                                "finish": _label(start, finish), "late_minutes": finish - order["due"],
                                "reason": reason})

    utilization = {}
    for machine in machines:
        available = finish_minute - sum(max(0, min(b, finish_minute) - min(a, finish_minute))
                                        for a, b in machine["down_times"] if a < finish_minute)
        busy = sum(r["end_minute"] - r["start_minute"] for r in plan[machine["name"]])
        utilization[machine["name"]] = round(100 * busy / available, 1) if available > 0 else 0.0
    lines = ["gantt", "    dateFormat  YYYY-MM-DD HH:mm", "    axisFormat  %H:%M"]
    safe = lambda value: re.sub(r"[^A-Za-z0-9_ -]", "", value)
    for machine in machines:
        lines.append(f"    section {safe(machine['name'])}")
        for index, record in enumerate(plan[machine["name"]]):
            title = safe(f"{record['order']} step {record['step']}")
            lines.append(f"    {title} :{machine['name']}_{index}, {record['start']}, {record['end']}")
    return {"status": final_status, "plan": plan, "finish_time": _label(start, finish_minute),
            "makespan_hours": round(finish_minute / MINUTES_PER_HOUR, 2),
            "late_orders": late_orders, "machine_utilization_percent": utilization,
            "gantt": "\n".join(lines)}


@mcp.tool()
def solve_schedule(problem: dict[str, Any]) -> dict[str, Any]:
    """Schedule ordered shop-floor work across machines and machine downtime.

    Provide machines (name and optional down_times in hours) and orders (name,
    optional due_hour and priority, plus ordered steps with hours and eligible machines).
    """
    return _solve_schedule_core(problem)


@mcp.tool()
def get_example() -> dict[str, Any]:
    """Return a small example shop with CNC and assembly orders."""
    return {
        "start": DEFAULT_START,
        "machines": [
            {"name": "CNC-1"}, {"name": "CNC-2", "down_times": [[3, 6]]},
            {"name": "CNC-3"}, {"name": "Assembly-1"}, {"name": "Assembly-2"},
        ],
        "orders": [
            {"name": "Urgent pump", "due_hour": 5, "priority": 10,
             "steps": [{"hours": 1.5, "machines": ["CNC-1", "CNC-2"]}, {"hours": 2, "machines": ["Assembly-1"]}]},
            {"name": "Valve A", "due_hour": 8, "steps": [{"hours": 2, "machines": ["CNC-1", "CNC-2", "CNC-3"]}, {"hours": 1, "machines": ["Assembly-1", "Assembly-2"]}]},
            {"name": "Valve B", "due_hour": 10, "steps": [{"hours": 1.5, "machines": ["CNC-1", "CNC-2", "CNC-3"]}, {"hours": 1, "machines": ["Assembly-1", "Assembly-2"]}]},
            {"name": "Bracket A", "due_hour": 9, "steps": [{"hours": 2.5, "machines": ["CNC-1", "CNC-2", "CNC-3"]}]},
            {"name": "Bracket B", "due_hour": 11, "steps": [{"hours": 2, "machines": ["CNC-1", "CNC-2", "CNC-3"]}]},
            {"name": "Housing A", "due_hour": 4, "steps": [{"hours": 3, "machines": ["CNC-1", "CNC-3"]}, {"hours": 1.5, "machines": ["Assembly-1"]}]},
            {"name": "Housing B", "due_hour": 14, "steps": [{"hours": 2.5, "machines": ["CNC-1", "CNC-2", "CNC-3"]}, {"hours": 1, "machines": ["Assembly-1", "Assembly-2"]}]},
            {"name": "Flange A", "due_hour": 15, "steps": [{"hours": 2, "machines": ["CNC-1", "CNC-2", "CNC-3"]}]},
            {"name": "Flange B", "due_hour": 16, "steps": [{"hours": 1.5, "machines": ["CNC-1", "CNC-2", "CNC-3"]}]},
            {"name": "Panel", "due_hour": 18, "steps": [{"hours": 2, "machines": ["CNC-2", "CNC-3"]}, {"hours": 1.5, "machines": ["Assembly-1", "Assembly-2"]}]},
        ],
    }


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
