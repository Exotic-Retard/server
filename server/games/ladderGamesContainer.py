import random


from PySide.QtSql import QSqlQuery
import asyncio
import trueskill
import config

from .gamesContainer import  GamesContainer
from .ladderGame import Ladder1V1Game
import server
from server.players import Player, PlayerState
import server.db as db


class Ladder1V1GamesContainer(GamesContainer):
    """Class for 1vs1 ladder games"""
    listable = False

    def __init__(self, db, games_service, name='ladder1v1', nice_name='ladder 1 vs 1'):
        super(Ladder1V1GamesContainer, self).__init__(name, nice_name, db, games_service)

        self.players = []
        self.host = False
        self.join = False
        self.games_service = games_service

    @asyncio.coroutine
    def getLeague(self, season, player):
        with (yield from db.db_pool) as conn:
            with (yield from conn.cursor()) as cursor:
                yield from cursor.execute("SELECT league FROM %s WHERE idUser = %s", (season, player.id))
                (league, ) = yield from cursor.fetchone()
                if league:
                    return league

    @asyncio.coroutine
    def addPlayer(self, player):
        if player not in self.players:
            league = yield from self.getLeague(config.LADDER_SEASON, player)
            if not league:
                with (yield from db.db_pool) as conn:
                    with (yield from conn.cursor()) as cursor:
                        yield from cursor.execute("INSERT INTO %s (`idUser` ,`league` ,`score`) "
                                                  "VALUES (%s, 1, 0)", (config.LADDER_SEASON, player.id))

            player.league = league

            self.players.append(player)
            player.state = PlayerState.SEARCHING_LADDER
            mean, deviation = player.ladder_rating

            if deviation > 490:
                player.lobbyThread.sendJSON(dict(command="notice", style="info", text="<i>Welcome to the matchmaker system.</i><br><br><b>You will be randomnly matched until the system learn and know enough about you.</b><br>After that, you will be only matched against someone of your level.<br><br><b>So don't worry if your first games are uneven, this will get better over time !</b>"))
            elif deviation > 250:
                progress = (500.0 - deviation) / 2.5
                player.lobbyThread.sendJSON(dict(command="notice", style="info", text="The system is still learning you. <b><br><br>The learning phase is " + str(progress)+"% complete<b>"))
            
            return 1
        return 0

    def removePlayer(self, player):
        if player in self.players:
            self.players.remove(player)
            player.state = PlayerState.IDLE
            return 1
        return 0
    
    def getMatchQuality(self, player1: Player, player2: Player):
        return trueskill.quality_1vs1(player1.ladder_rating, player2.ladder_rating)

    @asyncio.coroutine
    def selected_maps(self, playerId):
        with (yield from db.db_pool) as conn:
            with(yield from conn.cursor()) as cursor:
                yield from cursor.execute("SELECT idMap FROM ladder_map_selection WHERE idUser = %s", playerId)
                return set((yield from cursor.fetchall()))

    @asyncio.coroutine
    def popular_maps(self, count=50):
        with (yield from db.db_pool) as conn:
            with (yield from conn.cursor()) as cursor:
                yield from cursor.execute("SELECT `idMap` FROM `ladder_map_selection` GROUP BY `idMap` ORDER BY count(`idUser`) DESC LIMIT %i" % count)
                return set(cursor.fetchall())

    @asyncio.coroutine
    def getMapName(self, mapId):
        with (yield from db.db_pool) as conn:
            with (yield from conn.cursor()) as cursor:
                yield from cursor.execute("SELECT filename FROM table_map WHERE id = %s", (mapId))
                (name, ) = yield from cursor.fetchone()
                return str(name).split("/")[1].replace(".zip", "")

    @asyncio.coroutine
    def get_recent_maps(self, player1, player2, count=5):
        """
        Find the `count` most recently played maps from players
        """
        with (yield from db.db_pool) as conn:
            with (yield from conn.cursor()) as cursor:
                yield from cursor.execute('(SELECT game_stats.mapId FROM game_player_stats '
                                          'INNER JOIN game_stats on game_stats.id = game_player_stats.gameId '
                                          'WHERE game_player_stats.playerId = %s '
                                          'ORDER BY gameId DESC LIMIT %s) '
                                          'UNION DISTINCT '
                                          '(SELECT game_stats.mapId FROM game_player_stats '
                                          'INNER JOIN game_stats on game_stats.id = game_player_stats.gameId '
                                          'WHERE game_player_stats.playerId = %s '
                                          'ORDER BY gameID DESC LIMIT %s)', (player1.id, count, player2.id, count))
                return set((yield from cursor.fetchall()))

    @asyncio.coroutine
    def choose_ladder_map_pool(self, player1, player2):
        potential_maps = [
            (yield from self.selected_maps(player1.id)),
            (yield from self.selected_maps(player2.id)),
            (yield from self.popular_maps())
        ]

        pool = potential_maps[0] & potential_maps[1]

        if len(pool) < 15:
            expansion_pool = random.choice(potential_maps) - pool
            pool |= set(random.sample(expansion_pool, min(len(expansion_pool), 15 - len(pool))))

        if len(pool) < 15:
            expansion_pool = potential_maps[2] - pool
            pool |= set(random.sample(expansion_pool, min(len(expansion_pool), 15 - len(pool))))

        # Invariant: len(pool) >= 15, given that any potential pool has 15 or more
        # Since we select from top 50 popular maps, this should hold

        return pool - (yield from self.get_recent_maps(player1, player2))

    @asyncio.coroutine
    def startGame(self, player1, player2):
        gameName = str(player1.login + " Vs " + player2.login)
        
        player1.state = PlayerState.HOSTING
        gameid = self.games_service.createUuid()
        player2.state = PlayerState.JOINING

        map_pool = yield from self.choose_ladder_map_pool(player1, player2)

        mapChosen = random.choice(tuple(map_pool))
        map = self.getMapName(mapChosen)

        ngame = Ladder1V1Game(gameid, self)
        ngame.game_mode = self.game_mode
        id = ngame.id

        player1.setGame(id)
        player2.setGame(id)

        #host is player 1
        
        ngame.setGameMap(map)
        
        ngame.host = player1
        ngame.name = gameName

        ngame.set_player_option(player1.id, 'StartSpot', 1)
        ngame.set_player_option(player2.id, 'StartSpot', 2)
        ngame.set_player_option(player1.id, 'Team', 1)
        ngame.set_player_option(player2.id, 'Team', 2)

        ngame.addPlayerToJoin(player2)

        ngame.setLeaguePlayer(player1)
        ngame.setLeaguePlayer(player2)

        # player 2 will be in game
        
        self.addGame(ngame)

        #warn both players
        json = {}
        json["command"] = "game_launch"
        json["mod"] = self.game_mode
        json["mapname"] = str(map)
        json["reason"] = "ranked"
        json["uid"] = id
        json["args"] = ["/players 2", "/team 1"]
        
        player1.lobbyThread.sendJSON(json)
