from production_scheduling_mcp.server import get_example, solve_schedule

result = solve_schedule(get_example())
assert result["status"] in {"OPTIMAL", "FEASIBLE"}, result
late_orders = result["late_orders"]
assert len(late_orders) == 1, late_orders
assert late_orders[0]["name"] != "Urgent pump", late_orders
print("status:", result["status"])
print("makespan:", result["makespan_hours"], "hours")
print("late order:", late_orders[0]["name"])
print("reason:", late_orders[0]["reason"])
