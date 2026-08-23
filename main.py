OpenCV    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

# --- Pydantic Data Models ---
class Card(BaseModel):
    title: str
    phrase: str
    category: str
    icon: Optional[str] = None
    priority: str = "medium"

class CardUpdate(BaseModel):
    title: Optional[str] = None
    phrase: Optional[str] = None
    category: Optional[str] = None
    icon: Optional[str] = None
    priority: Optional[str] = None

class RequestEvent(BaseModel):
    message: str
    category: str
    input_method: str = "Touch Grid"
    priority: str = "medium"
    patient_id: Optional[str] = "Default Patient"
    room_number: Optional[str] = "Room 101"

class EmergencyEvent(BaseModel):
    message: str = "Emergency assistance requested!"
    input_method: str = "Emergency Button"
    patient_id: Optional[str] = "Default Patient"
    room_number: Optional[str] = "Room 101"

class StatusUpdate(BaseModel):
    status: str = "Completed"
    resolved_by: Optional[str] = "Caregiver"
    notes: Optional[str] = None

class PatientProfile(BaseModel):
    name: str
    room_number: str
    condition_summary: Optional[str] = None
    input_preference: str = "Touch Grid"
    dwell_time_seconds: Optional[float] = 1.5
    emergency_contact: Optional[str] = None

# --- Web Frontend & Health Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def serve_home():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    return {"status": "backend is running", "version": "1.2.0"}

@app.get("/api/status")
async def root():
    return {
        "status": "backend is running",
        "version": "1.2.0",
        "active_ws_clients": len(manager.active_connections)
    }

@app.get("/ping-db")
async def ping_db():
    try:
        await db.command("ping")
        return {"mongo": "connected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")

# --- Phrase Cards Endpoints ---
@app.get("/cards")
async def get_cards(category: Optional[str] = None):
    query = {"category": category} if category else {}
    cards = []
    async for card in cards_collection.find(query):
        card["_id"] = str(card["_id"])
        card["id"] = str(card["_id"])
        cards.append(card)
    return cards

@app.post("/cards")
async def create_card(card: Card):
    result = await cards_collection.insert_one(card.model_dump())
    new_card = card.model_dump()
    new_card["_id"] = str(result.inserted_id)
    new_card["id"] = str(result.inserted_id)
    await manager.broadcast("card_created", new_card)
    return {"id": str(result.inserted_id), "_id": str(result.inserted_id)}

@app.put("/cards/{card_id}")
async def update_card(card_id: str, card_update: CardUpdate):
    try:
        oid = ObjectId(card_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid card ID format")
    
    update_data = {k: v for k, v in card_update.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    
    result = await cards_collection.update_one(
        {"_id": oid},
        {"$set": update_data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Card not found")
    
    await manager.broadcast("card_updated", {"id": card_id, "updated": update_data})
    return {"updated": True}

@app.delete("/cards/{card_id}")
async def delete_card(card_id: str):
    try:
        oid = ObjectId(card_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid card ID format")
    
    result = await cards_collection.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Card not found")
    
    await manager.broadcast("card_deleted", {"id": card_id})
    return {"deleted": True}

# --- Request & Emergency Endpoints ---
@app.post("/requests")
async def create_request(req: RequestEvent):
    created_at = datetime.utcnow()
    doc = req.model_dump()
    doc["status"] = "Pending"
    doc["created_at"] = created_at
    result = await requests_collection.insert_one(doc)
    
    broadcast_doc = {
        "id": str(result.inserted_id),
        "_id": str(result.inserted_id),
        "message": doc["message"],
        "category": doc["category"],
        "input_method": doc["input_method"],
        "priority": doc["priority"],
        "status": doc["status"],
        "patient_id": doc.get("patient_id"),
        "room_number": doc.get("room_number"),
        "created_at": created_at.isoformat()
    }
    await manager.broadcast("new_request", broadcast_doc)
    return {"id": str(result.inserted_id), "_id": str(result.inserted_id)}

@app.post("/requests/emergency")
async def create_emergency_request(req: EmergencyEvent = EmergencyEvent()):
    created_at = datetime.utcnow()
    doc = {
        "message": req.message,
        "category": "Emergency",
        "input_method": req.input_method,
        "priority": "urgent",
        "status": "Pending",
        "patient_id": req.patient_id,
        "room_number": req.room_number,
        "created_at": created_at
    }
    result = await requests_collection.insert_one(doc)
    
    broadcast_doc = {
        "id": str(result.inserted_id),
        "_id": str(result.inserted_id),
        "message": doc["message"],
        "category": "Emergency",
        "input_method": doc["input_method"],
        "priority": "urgent",
        "status": "Pending",
        "patient_id": req.patient_id,
        "room_number": req.room_number,
        "created_at": created_at.isoformat()
    }
    await manager.broadcast("emergency_alert", broadcast_doc)
    return {"id": str(result.inserted_id), "_id": str(result.inserted_id), "priority": "urgent"}

@app.get("/requests")
async def get_requests(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = Query(default=50, le=200)
):
    query = {}
    if status and status.lower() != "all":
        query["status"] = status
    if priority and priority.lower() != "all":
        query["priority"] = priority
    
    results = []
    raw_docs = await requests_collection.find(query).sort("created_at", -1).to_list(limit)
    for r in raw_docs:
        r["_id"] = str(r["_id"])
        r["id"] = str(r["_id"])
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
        if isinstance(r.get("resolved_at"), datetime):
            r["resolved_at"] = r["resolved_at"].isoformat()
        results.append(r)
    return results

@app.patch("/requests/{request_id}")
@app.put("/requests/{request_id}")
async def update_request_status(request_id: str, update: StatusUpdate):
    try:
        oid = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID format")
    
    status_val = update.status or "Completed"
    update_fields = {"status": status_val}
    if status_val.lower() in ["completed", "resolved", "dismissed"]:
        update_fields["resolved_at"] = datetime.utcnow()
    if update.resolved_by:
        update_fields["resolved_by"] = update.resolved_by
    if update.notes:
        update_fields["notes"] = update.notes

    result = await requests_collection.update_one(
        {"_id": oid},
        {"$set": update_fields}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Request not found")
    
    await manager.broadcast("request_status_updated", {
        "id": request_id,
        "_id": request_id,
        "status": status_val,
        "details": {
            "status": status_val,
            "resolved_by": update.resolved_by,
            "notes": update.notes
        }
    })
    return {"updated": True, "id": request_id, "status": status_val}

@app.delete("/requests/all-dev-clear")
async def clear_all_requests():
    result = await requests_collection.delete_many({})
    await manager.broadcast("all_requests_cleared", {"deleted_count": result.deleted_count})
    return {"deleted_count": result.deleted_count}

# --- Caregiver Analytics & Metrics Dashboard ---
@app.get("/analytics/stats")
async def get_analytics_stats():
    total_requests = await requests_collection.count_documents({})
    pending_requests = await requests_collection.count_documents({"status": "Pending"})
    completed_requests = await requests_collection.count_documents({"status": {"$in": ["Completed", "Resolved"]}})
    urgent_requests = await requests_collection.count_documents({"priority": "urgent"})

    category_pipeline = [
        {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    category_stats = await requests_collection.aggregate(category_pipeline).to_list(20)
    category_breakdown = {item["_id"]: item["count"] for item in category_stats if item.get("_id")}

    priority_pipeline = [
        {"$group": {"_id": "$priority", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    priority_stats = await requests_collection.aggregate(priority_pipeline).to_list(10)
    priority_breakdown = {item["_id"]: item["count"] for item in priority_stats if item.get("_id")}

    return {
        "total_requests": total_requests,
        "pending_requests": pending_requests,
        "completed_requests": completed_requests,
        "urgent_requests": urgent_requests,
        "category_breakdown": category_breakdown,
        "priority_breakdown": priority_breakdown
    }

# --- Patient Profile Management ---
@app.get("/profiles")
async def get_profiles():
    profiles = []
    async for p in profiles_collection.find():
        p["_id"] = str(p["_id"])
        p["id"] = str(p["_id"])
        profiles.append(p)
    return profiles

@app.post("/profiles")
async def create_profile(profile: PatientProfile):
    result = await profiles_collection.insert_one(profile.model_dump())
    return {"id": str(result.inserted_id), "_id": str(result.inserted_id)}
